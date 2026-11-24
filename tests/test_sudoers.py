"""Tests for the sudoers setup. ONLY fake binaries are used: SUDO_BIN, PKEXEC_BIN,
VISUDO_BIN and SSHUTTLE_BIN always point at scripts in a temp dir. The real
sudo/pkexec/visudo must never run, and nothing is written under /etc."""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ctl  # noqa: E402

RULE = "alice ALL=(root) NOPASSWD: /usr/bin/sshuttle-fw\n"
TARGET = "/etc/sudoers.d/sshuttle-panel"


def script(path, body):
    Path(path).write_text("#!/bin/sh\n" + body)
    os.chmod(path, 0o755)
    return str(path)


class SudoBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.pk_log = t / "pkexec.argv"
        self.pk_copy = t / "pkexec.rule"
        self.fakes = {
            "SSHUTTLE_BIN": script(t / "sshuttle", f"printf %s '{RULE}'\n"),
            "VISUDO_BIN": script(t / "visudo", "exit 0\n"),
            # records argv one per line and its stdin
            "PKEXEC_BIN": script(
                t / "pkexec",
                f'for a in "$@"; do printf "%s\\n" "$a"; done > {self.pk_log}\n'
                f"cat > {self.pk_copy}\n"
                "exit 0\n"),
            "SUDO_BIN": script(t / "sudo", "exit 0\n"),
            "SSHUTTLE_PANEL_RUNTIME_DIR": str(t / "rt"),
            "SSHUTTLE_PANEL_CONFIG_DIR": str(t / "cfg"),
        }
        p = mock.patch.dict(os.environ, self.fakes)
        p.start()
        self.addCleanup(p.stop)

    def replace(self, name, body):
        script(self.fakes[name], body)

    def pk_argv(self):
        return self.pk_log.read_text().splitlines()


class TestSudoReady(SudoBase):
    def test_sudo_ready_true_and_false(self):
        self.assertTrue(ctl.sudo_ready(self.fakes["SSHUTTLE_BIN"]))
        self.replace("SUDO_BIN", "exit 1\n")
        self.assertFalse(ctl.sudo_ready(self.fakes["SSHUTTLE_BIN"]))

    def test_sudo_ready_passes_n_l_and_resolved_path(self):
        out = Path(self.tmp.name) / "sudo.argv"
        self.replace("SUDO_BIN", f'printf "%s\\n" "$@" > {out}\nexit 0\n')
        ctl.sudo_ready(self.fakes["SSHUTTLE_BIN"])
        self.assertEqual(out.read_text().splitlines(),
                         ["-n", "-l", self.fakes["SSHUTTLE_BIN"]])

    def test_sudo_ready_false_when_sudo_missing_or_sshuttle_missing(self):
        with mock.patch.dict(os.environ, {"SUDO_BIN": "/nonexistent/sudo"}):
            self.assertFalse(ctl.sudo_ready(self.fakes["SSHUTTLE_BIN"]))
        self.assertFalse(ctl.sudo_ready("/nonexistent/sshuttle"))

    def test_status_reports_sudo_ready(self):
        self.assertIs(ctl.status()["sudo_ready"], True)
        self.replace("SUDO_BIN", "exit 1\n")
        self.assertIs(ctl.status()["sudo_ready"], False)


class TestSetupSudo(SudoBase):
    def test_setup_sudo_installs_validated_rule(self):
        res = ctl.setup_sudo()
        self.assertTrue(res["ok"], res)
        argv = self.pk_argv()
        self.assertEqual(argv, ["install", "-m", "0440", "/dev/stdin", TARGET])
        self.assertEqual(self.pk_copy.read_bytes(), RULE.encode())  # validated bytes via stdin

    def test_setup_sudo_passes_user_to_sshuttle(self):
        out = Path(self.tmp.name) / "sshuttle.argv"
        self.replace("SSHUTTLE_BIN", f'printf "%s\\n" "$@" > {out}\nprintf %s \'{RULE}\'\n')
        ctl.setup_sudo()
        import getpass
        self.assertEqual(out.read_text().splitlines(),
                         ["--sudoers-no-modify", "--sudoers-user", getpass.getuser()])

    def test_setup_sudo_visudo_checks_the_file(self):
        out = Path(self.tmp.name) / "visudo.argv"
        self.replace("VISUDO_BIN", f'printf "%s\\n" "$@" > {out}\nexit 0\n')
        ctl.setup_sudo()
        a = out.read_text().splitlines()
        self.assertEqual(a[:2], ["-c", "-f"])
        self.assertNotIn(a[2], self.pk_argv())  # privileged step never sees the temp path

    def test_setup_sudo_rejects_when_visudo_fails(self):
        self.replace("VISUDO_BIN", "echo bad syntax >&2\nexit 1\n")
        res = ctl.setup_sudo()
        self.assertFalse(res["ok"])
        self.assertIn("validation", res["error"])
        self.assertFalse(self.pk_log.exists())  # pkexec never called

    def test_setup_sudo_cancelled(self):
        self.replace("PKEXEC_BIN", "exit 126\n")
        res = ctl.setup_sudo()
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "Authorization cancelled")

    def test_setup_sudo_pkexec_other_failure(self):
        self.replace("PKEXEC_BIN", "exit 127\n")
        res = ctl.setup_sudo()
        self.assertFalse(res["ok"])
        self.assertNotEqual(res["error"], "Authorization cancelled")

    def test_setup_sudo_sshuttle_failure_or_empty_output(self):
        self.replace("SSHUTTLE_BIN", "exit 2\n")
        self.assertFalse(ctl.setup_sudo()["ok"])
        self.replace("SSHUTTLE_BIN", "exit 0\n")  # empty rule
        self.assertFalse(ctl.setup_sudo()["ok"])
        self.assertFalse(self.pk_log.exists())

    def test_setup_sudo_missing_binaries(self):
        for name in ("SSHUTTLE_BIN", "VISUDO_BIN", "PKEXEC_BIN"):
            with mock.patch.dict(os.environ, {name: "/nonexistent/x"}):
                self.assertFalse(ctl.setup_sudo()["ok"], name)
        self.assertFalse(self.pk_log.exists())

    def test_tempfile_and_dir_private_and_removed(self):
        out = Path(self.tmp.name) / "visudo.info"
        self.replace("VISUDO_BIN",
                     f'f="$3"; stat -c "%a" "$f" "$(dirname "$f")" > {out}; '
                     f'dirname "$f" >> {out}\nexit 0\n')
        ctl.setup_sudo()
        fmode, dmode, d = out.read_text().split()
        self.assertEqual((fmode, dmode), ("600", "700"))
        self.assertFalse(os.path.exists(d))

    def test_tempfile_modified_after_validation_cannot_influence_install(self):
        self.replace("VISUDO_BIN", 'printf "evil ALL=(ALL) NOPASSWD: ALL\\n" >> "$3"\nexit 0\n')
        res = ctl.setup_sudo()
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.pk_copy.read_bytes(), RULE.encode())

    def test_cli_setup_sudo(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = ctl.main(["setup-sudo"])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(buf.getvalue())["ok"])
        self.replace("PKEXEC_BIN", "exit 126\n")
        with redirect_stdout(buf):
            self.assertEqual(ctl.main(["setup-sudo"]), 1)


if __name__ == "__main__":
    unittest.main()
