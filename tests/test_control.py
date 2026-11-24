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
