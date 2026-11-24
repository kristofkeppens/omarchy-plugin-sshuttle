import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ctl  # noqa: E402

FAKE = str(Path(__file__).resolve().parent / "fake_sshuttle.py")


def wait_for(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        val = pred()
        if val:
            return val
        time.sleep(0.02)
    return pred()


class Base(unittest.TestCase):
    mode = "ok"

    def setUp(self):
        self.cfg = tempfile.TemporaryDirectory()
        self.run_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.cfg.cleanup)
        self.addCleanup(self.run_dir.cleanup)
        self.argv_file = os.path.join(self.run_dir.name, "argv.json")
        p = mock.patch.dict(os.environ, {
            "SSHUTTLE_PANEL_CONFIG_DIR": self.cfg.name,
            "SSHUTTLE_PANEL_RUNTIME_DIR": os.path.join(self.run_dir.name, "rt"),
            "SSHUTTLE_BIN": FAKE,
            "SUDO_BIN": "/bin/false",  # never run the real sudo
            "FAKE_MODE": self.mode,
            "FAKE_ARGV_FILE": self.argv_file,
        })
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(ctl.stop)  # runs before p.stop (LIFO)
        ctl.save_profile({"name": "w", "remote": "me@h", "subnets": ["10.0.0.0/8"]})

    def state_path(self):
        return ctl.runtime_dir() / "state.json"


class TestOk(Base):
    def test_start_then_connected(self):
        res = ctl.start("w")
        self.assertTrue(res["ok"])
        st = wait_for(lambda: (s := ctl.status())["state"] == "connected" and s)
        self.assertTrue(st)
        self.assertEqual(st["profile"], "w")
        self.assertEqual(st["pid"], res["pid"])
        self.assertTrue(st["sshuttle_installed"])
        self.assertIs(st["sudo_ready"], False)
        self.assertIsNone(st["error"])
        self.assertIsInstance(st["since"], float)
        prof = ctl.load_store()["profiles"][0]
        self.assertEqual(json.loads(Path(self.argv_file).read_text()),
                         ctl.build_argv(prof, FAKE))

    def test_stop_returns_to_disconnected(self):
        pid = ctl.start("w")["pid"]
        wait_for(lambda: ctl.status()["state"] == "connected")
        self.assertEqual(ctl.stop(), {"ok": True})
        st = ctl.status()
        self.assertEqual(st["state"], "disconnected")
        self.assertIsNone(st["pid"])
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertFalse(self.state_path().exists())

    def test_stop_when_not_running_is_ok(self):
        self.assertEqual(ctl.stop(), {"ok": True})

    def test_start_twice_rejected(self):
        ctl.start("w")
        with self.assertRaises(ctl.ProfileError):
            ctl.start("w")

    def test_start_unknown_profile_rejected(self):
        with self.assertRaises(ctl.ProfileError):
            ctl.start("nope")
        self.assertEqual(ctl.status()["state"], "disconnected")

    def test_start_without_default_rejected(self):
        with self.assertRaises(ctl.ProfileError):
            ctl.start(None)

    def test_start_uses_default_when_no_name(self):
        ctl.set_default("w")
        ctl.start(None)
        st = wait_for(lambda: (s := ctl.status())["state"] == "connected" and s)
        self.assertEqual(st["profile"], "w")

    def test_log_returns_tail(self):
        ctl.start("w")
        wait_for(lambda: ctl.status()["state"] == "connected")
        self.assertIn("c : Connected to server.", ctl.read_log())

    def test_log_limited_to_50_lines(self):
        ctl.runtime_dir().mkdir(parents=True)
        (ctl.runtime_dir() / "sshuttle.log").write_text(
            "".join(f"l{i}\n" for i in range(80)))
        lines = ctl.read_log()
        self.assertEqual(len(lines), 50)
        self.assertEqual(lines[-1], "l79")

    def test_log_missing_is_empty(self):
        self.assertEqual(ctl.read_log(), [])


class TestSilent(Base):
    mode = "silent"

    def test_connecting_until_marker(self):
        ctl.start("w")
        time.sleep(0.2)
        st = ctl.status()
        self.assertEqual(st["state"], "connecting")
        self.assertEqual(st["profile"], "w")

    def test_connecting_timeout_becomes_error(self):
        with mock.patch.dict(os.environ, {"SSHUTTLE_PANEL_CONNECT_TIMEOUT": "1"}):
            pid = ctl.start("w")["pid"]
            st = wait_for(lambda: (s := ctl.status())["state"] == "error" and s, 4)
        self.assertTrue(st)
        self.assertIn("timed out", st["error"])
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        # error persists until stop
        self.assertEqual(ctl.status()["state"], "error")
        ctl.stop()
        self.assertEqual(ctl.status()["state"], "disconnected")


class TestDie(Base):
    mode = "die"

    def test_process_dies_reports_error_with_log_tail(self):
        ctl.start("w")
        st = wait_for(lambda: (s := ctl.status())["state"] == "error" and s)
        self.assertTrue(st)
        self.assertIn("Permission denied (publickey).", st["error"])
        self.assertEqual(ctl.status()["state"], "error")


class TestFirewallFailure(Base):
    mode = "fwfail"

    def test_fatal_after_handshake_is_error_not_disconnected(self):
        ctl.start("w")
        st = wait_for(lambda: (s := ctl.status())["state"] == "error" and s)
        self.assertTrue(st, ctl.status())
        self.assertEqual(st["error"], "c : fatal: ['nft', 'add', 'table'] returned 1")
        self.assertEqual(ctl.status()["state"], "error")  # sticky until stop
        ctl.stop()
        self.assertEqual(ctl.status()["state"], "disconnected")


class TestStale(Base):
    def test_stale_state_file_is_disconnected(self):
        ctl.runtime_dir().mkdir(parents=True)
        # dead pid
        import subprocess
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        self.state_path().write_text(json.dumps(
            {"pid": p.pid, "profile": "w", "started": time.time()}))
        self.assertEqual(ctl.status()["state"], "disconnected")
        self.assertFalse(self.state_path().exists())
        # alive pid that is not sshuttle (pid reuse)
        self.state_path().write_text(json.dumps(
            {"pid": os.getpid(), "profile": "w", "started": time.time()}))
        st = ctl.status()
        self.assertEqual(st["state"], "disconnected")
        self.assertIsNone(st["pid"])
        self.assertFalse(self.state_path().exists())
        # stop must not signal an unrelated process either
        self.state_path().write_text(json.dumps(
            {"pid": os.getpid(), "profile": "w", "started": time.time()}))
        ctl.stop()
        self.assertFalse(self.state_path().exists())

    def test_corrupt_state_file_is_disconnected(self):
        ctl.runtime_dir().mkdir(parents=True)
        for content in ["", "{nope", "[]", '{"pid": "x"}']:
            self.state_path().write_text(content)
            self.assertEqual(ctl.status()["state"], "disconnected")


class TestHardening(Base):
    mode = "silent"

    def _spawn(self, argv):
        import subprocess
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        return p

    def _write_state(self, **kw):
        ctl.runtime_dir().mkdir(parents=True, exist_ok=True)
        self.state_path().write_text(json.dumps(
            {"pid": 1234567, "profile": "w", "started": time.time(), **kw}))

    def _log(self, text):
        ctl.runtime_dir().mkdir(parents=True, exist_ok=True)
        (ctl.runtime_dir() / "sshuttle.log").write_text(text)

    def test_connected_then_gone_is_disconnected_not_error(self):
        self._write_state(connected=True)
        self._log("c : Connected to server.\nclient: fw: something odd\n")
        st = ctl.status()
        self.assertEqual(st["state"], "disconnected")
        self.assertIsNone(st["error"])
        self.assertFalse(self.state_path().exists())

    def test_connected_marker_in_log_then_gone_is_disconnected(self):
        self._write_state()  # connected flag never persisted
        self._log("c : Connected to server.\nTerminated\n")
        self.assertEqual(ctl.status()["state"], "disconnected")

    def test_fatal_after_marker_then_gone_is_error(self):
        for flag in ({}, {"connected": True}):
            with self.subTest(flag):
                self._write_state(**flag)
                self._log("c : Connected to server.\nc : fatal: failed to set up nft\n")
                st = ctl.status()
                self.assertEqual(st["state"], "error")
                self.assertEqual(st["error"], "c : fatal: failed to set up nft")
                self.assertEqual(json.loads(self.state_path().read_text())["error"], st["error"])
                ctl.stop()

    def test_traceback_after_marker_then_gone_reports_last_line(self):
        self._write_state(connected=True)
        self._log("c : Connected to server.\nTraceback (most recent call last):\n"
                  '  File "x", line 1\nOSError: [Errno 1] Operation not permitted\n\n')
        st = ctl.status()
        self.assertEqual(st["state"], "error")
        self.assertEqual(st["error"], "OSError: [Errno 1] Operation not permitted")

    def test_fatal_before_last_marker_is_ignored(self):
        self._write_state(connected=True)
        self._log("c : fatal: old\nc : Connected to server.\nTerminated\n")
        self.assertEqual(ctl.status()["state"], "disconnected")

    def test_fatal_error_line_is_clipped(self):
        self._write_state(connected=True)
        self._log("c : Connected to server.\nc : fatal: " + "x" * 2000 + "\n")
        st = ctl.status()
        self.assertEqual(st["state"], "error")
        self.assertLessEqual(len(st["error"]), 500)

    def test_fatal_after_marker_but_foreign_pid_is_disconnected(self):
        self._write_state(connected=True)
        st = json.loads(self.state_path().read_text())
        self.state_path().write_text(json.dumps({**st, "pid": os.getpid()}))
        self._log("c : Connected to server.\nc : fatal: x\n")
        self.assertEqual(ctl.status()["state"], "disconnected")

    def test_never_connected_then_gone_is_error(self):
        self._write_state()
        self._log("Permission denied (publickey).\n")
        st = ctl.status()
        self.assertEqual(st["state"], "error")
        self.assertIn("Permission denied", st["error"])

    def test_pid_reuse_with_log_present_is_disconnected(self):
        self._log("Permission denied (publickey).\n")
        self._write_state()
        st = json.loads(self.state_path().read_text())
        st["pid"] = os.getpid()
        self.state_path().write_text(json.dumps(st))
        out = ctl.status()
        self.assertEqual(out["state"], "disconnected")
        self.assertIsNone(out["pid"])
        self.assertFalse(self.state_path().exists())

    def test_alive_permission_error_means_exists(self):
        p = self._spawn([FAKE])
        time.sleep(0.2)
        with mock.patch.object(ctl.os, "kill", side_effect=PermissionError):
            self.assertTrue(ctl._alive(p.pid))

    def test_alive_proc_read_oserror_is_not_alive(self):
        with mock.patch.object(ctl.Path, "read_bytes", side_effect=PermissionError):
            self.assertFalse(ctl._alive(os.getpid()))

    def test_alive_matches_argv0_or_argv1_basename_only(self):
        p = self._spawn([sys.executable, "-c", "import time; time.sleep(30)", "sshuttle"])
        time.sleep(0.2)
        with mock.patch.dict(os.environ, {"SSHUTTLE_BIN": "/usr/bin/sshuttle"}):
            self.assertFalse(ctl._alive(p.pid))
        q = self._spawn([FAKE])  # python3 /path/fake_sshuttle.py
        time.sleep(0.2)
        self.assertTrue(ctl._alive(q.pid))
        # `env python3 script` (shebang before env has exec'd python)
        r = self._spawn(["env", sys.executable, FAKE])
        time.sleep(0.2)
        self.assertTrue(ctl._alive(r.pid))

    def test_concurrent_start_yields_one_process(self):
        import threading
        orig = ctl.status

        def slow_status():
            r = orig()
            time.sleep(0.3)
            return r

        results = []

        def run():
            try:
                results.append(("ok", ctl.start("w")["pid"]))
            except ctl.ProfileError as e:
                results.append(("err", str(e)))

        def reap():
            for kind, v in results:
                if kind == "ok":
                    try:
                        os.killpg(v, 9)
                    except OSError:
                        pass
        self.addCleanup(reap)
        with mock.patch.object(ctl, "status", slow_status):
            ts = [threading.Thread(target=run) for _ in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(sorted(k for k, _ in results), ["err", "ok"], results)
        pid = [v for k, v in results if k == "ok"][0]
        self.assertTrue(ctl._alive(pid))

    def test_log_reads_only_tail(self):
        self._log("".join(f"line{i}\n" for i in range(200000)))
        with mock.patch.object(ctl.Path, "read_bytes", side_effect=AssertionError("whole file read")):
            lines = ctl.read_log()
        self.assertEqual(lines[-1], "line199999")
        self.assertEqual(len(lines), 50)

    def test_log_file_mode_0600(self):
        ctl.start("w")
        mode = os.stat(ctl.runtime_dir() / "sshuttle.log").st_mode & 0o777
        self.assertEqual(mode, 0o600)


class TestSshFailureExplained(Base):
    """Show ssh's own reason, with a hint, not sshuttle's generic last line."""

    mode = "silent"
    _write_state = TestHardening._write_state
    _log = TestHardening._log

    REAL_LOG = (
        "Warning: Permanently added '[jump.example.com]:2222' (ED25519) to the list of known hosts.\n"
        "alice@jump.example.com: Permission denied (publickey).\n"
        "c : fatal: failed to establish ssh session (2)\n"
    )

    def _error(self, log):
        self._write_state()
        self._log(log)
        st = ctl.status()
        self.assertEqual(st["state"], "error")
        return st["error"]

    def test_publickey_failure_names_the_cause_and_the_passphrase_hint(self):
        msg = self._error(self.REAL_LOG)
        self.assertIn("Permission denied (publickey)", msg)
        self.assertIn("passphrase", msg)
        self.assertIn("ssh-add", msg)
        self.assertLessEqual(len(msg), 500)

    def test_host_key_failure_is_explained(self):
        msg = self._error("Host key verification failed.\nc : fatal: failed to establish ssh session (2)\n")
        self.assertIn("Host key verification failed", msg)

    def test_other_ssh_errors_show_the_ssh_line_not_the_generic_one(self):
        for line in ["ssh: connect to host h port 22: Connection refused",
                     "ssh: Could not resolve hostname nope: Name or service not known",
                     "ssh: connect to host h port 22: Connection timed out"]:
            with self.subTest(line=line):
                msg = self._error(f"{line}\nc : fatal: failed to establish ssh session (255)\n")
                self.assertIn(line.split(": ", 1)[1], msg)

    def test_unrecognised_failure_still_shows_last_line(self):
        msg = self._error("something unexpected happened\nc : fatal: boom\n")
        self.assertEqual(msg, "c : fatal: boom")

    def test_publickey_line_after_connect_is_not_misread(self):
        # Only the never-connected path is explained; a post-connect fatal is shown as is.
        self._write_state(connected=True)
        self._log("c : Connected to server.\nc : fatal: tproxy failed\n")
        self.assertEqual(ctl.status()["error"], "c : fatal: tproxy failed")


class TestSshAgentFallback(Base):
    """The shell can start before the ssh-agent exists, so it may lack
    SSH_AUTH_SOCK even though the user's terminal has it. Fall back to the
    systemd agent's standard socket in the user's runtime dir."""

    def setUp(self):
        super().setUp()
        import socket
        # AF_UNIX paths are limited to ~107 bytes; keep this directory short.
        self.xdg = tempfile.TemporaryDirectory(prefix="x", dir="/tmp")
        self.addCleanup(self.xdg.cleanup)
        self.sock_path = os.path.join(self.xdg.name, "ssh-agent.socket")
        self.env_file = os.path.join(self.run_dir.name, "env.json")
        p = mock.patch.dict(os.environ, {
            "XDG_RUNTIME_DIR": self.xdg.name, "FAKE_ENV_FILE": self.env_file, "FAKE_MODE": "ok"})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("SSH_AUTH_SOCK", None)
        self.socket = socket

    def _listen(self, path):
        s = self.socket.socket(self.socket.AF_UNIX)
        s.bind(path)
        self.addCleanup(s.close)

    def _seen(self):
        ctl.start("w")
        wait_for(lambda: os.path.exists(self.env_file) and os.path.getsize(self.env_file) > 0)
        with open(self.env_file) as f:
            return json.load(f)["SSH_AUTH_SOCK"]

    def test_uses_the_systemd_agent_socket_when_unset(self):
        self._listen(self.sock_path)
        self.assertEqual(self._seen(), self.sock_path)

    def test_an_existing_ssh_auth_sock_is_never_overridden(self):
        self._listen(self.sock_path)
        os.environ["SSH_AUTH_SOCK"] = "/somewhere/else.sock"
        self.assertEqual(self._seen(), "/somewhere/else.sock")

    def test_nothing_is_invented_when_there_is_no_agent_socket(self):
        self.assertIsNone(self._seen())

    def test_a_regular_file_is_not_mistaken_for_the_socket(self):
        Path(self.sock_path).touch()
        self.assertIsNone(self._seen())


class TestMissingBinary(Base):
    def test_missing_binary_is_error(self):
        with mock.patch.dict(os.environ, {"SSHUTTLE_BIN": "/nonexistent/sshuttle"}):
            st = ctl.status()
            self.assertIs(st["sshuttle_installed"], False)
            self.assertEqual(st["state"], "error")
            self.assertIn("sshuttle not found", st["error"])
            with self.assertRaises(ctl.ProfileError) as cm:
                ctl.start("w")
            self.assertIn("sshuttle not found", str(cm.exception))


class TestCli(Base):
    def run_main(self, args):
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            rc = ctl.main(args)
        return rc, json.loads(out.getvalue())

    def test_start_status_log_stop(self):
        rc, out = self.run_main(["start", "w"])
        self.assertEqual(rc, 0)
        self.assertIsInstance(out["pid"], int)
        wait_for(lambda: self.run_main(["status"])[1]["state"] == "connected")
        rc, out = self.run_main(["log"])
        self.assertEqual(rc, 0)
        self.assertIsInstance(out["lines"], list)
        rc, out = self.run_main(["start", "w"])
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])
        self.assertEqual(self.run_main(["stop"]), (0, {"ok": True}))
        self.assertEqual(self.run_main(["status"])[1]["state"], "disconnected")


if __name__ == "__main__":
    unittest.main()
