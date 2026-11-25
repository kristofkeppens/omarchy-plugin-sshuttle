import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ctl  # noqa: E402
from tests.test_control import Base, wait_for  # noqa: E402

OURS = ('0 0 192.0.2.10:34948 198.51.100.7:22 users:(("ssh",pid=4242,fd=3))\n'
        '\t bbr wscale:10,10 rto:228 mss:1448 cwnd:36 bytes_sent:52066 bytes_acked:52067 bytes_received:35501 segs_out:140\n')
OTHER = ('0 0 192.0.2.10:57406 203.0.113.9:443 users:(("chrome",pid=999,fd=40))\n'
         '\t bbr mss:1448 bytes_sent:99999999 bytes_acked:99999999 bytes_received:88888888 segs_out:5\n')


class TestParse(unittest.TestCase):
    def test_picks_our_connection_among_others(self):
        self.assertEqual(ctl.parse_ss_counters(OTHER + OURS, {4242}), (35501, 52066))

    def test_no_match(self):
        self.assertIsNone(ctl.parse_ss_counters(OTHER, {4242}))

    def test_empty(self):
        self.assertIsNone(ctl.parse_ss_counters("", {4242}))

    def test_garbage(self):
        self.assertIsNone(ctl.parse_ss_counters("hello\nworld", {4242}))

    def test_missing_bytes_received(self):
        text = OURS.replace("bytes_received:35501 ", "")
        self.assertIsNone(ctl.parse_ss_counters(text, {4242}))

    def test_record_owned_by_two_pids(self):
        text = OURS.replace('("ssh",pid=4242,fd=3)', '("a",pid=1,fd=3),("b",pid=4242,fd=4)')
        self.assertEqual(ctl.parse_ss_counters(text, {4242}), (35501, 52066))

    def test_largest_total_wins(self):
        big = OURS.replace("bytes_sent:52066", "bytes_sent:900000")
        small = OURS.replace("pid=4242", "pid=4243")
        self.assertEqual(ctl.parse_ss_counters(small + big, {4242, 4243}), (35501, 900000))

    def test_pid_is_not_a_substring_match(self):
        self.assertIsNone(ctl.parse_ss_counters(OURS, {424}))


class TestDescendants(unittest.TestCase):
    def test_finds_child_and_grandchild(self):
        p = subprocess.Popen(["sh", "-c", "sleep 30 & wait"], start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)

        def cleanup():
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.wait()
        self.addCleanup(cleanup)
        got = wait_for(lambda: len(ctl.descendant_pids(p.pid)) >= 1 and ctl.descendant_pids(p.pid))
        self.assertTrue(got)
        # sh -c may exec the last command or fork; either way sleep is a descendant
        found = ctl.descendant_pids(p.pid)
        self.assertNotIn(p.pid, found)
        sleeper = subprocess.run(["pgrep", "-g", str(p.pid), "-x", "sleep"],
                                 capture_output=True, text=True).stdout.split()
        self.assertTrue(sleeper)
        for s in sleeper:
            self.assertIn(int(s), found)

    def test_unknown_pid(self):
        self.assertEqual(ctl.descendant_pids(10**7), set())


class SsBase(Base):
    def setUp(self):
        super().setUp()
        d = self.run_dir.name
        self.canned = os.path.join(d, "canned.txt")
        self.log = os.path.join(d, "ss.log")
        self.fake_ss = os.path.join(d, "ss")
        self.write_ss(f'echo called >> "{self.log}"\ncat "{self.canned}"\n')
        self.set_canned(OTHER + OURS)
        self.set_env(SS_BIN=self.fake_ss)

    def set_env(self, **kw):
        p = mock.patch.dict(os.environ, kw)
        p.start()
        self.addCleanup(p.stop)

    def write_ss(self, body):
        with open(self.fake_ss, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(self.fake_ss, 0o755)

    def set_canned(self, text):
        with open(self.canned, "w") as f:
            f.write(text)

    def connect(self):
        self.assertTrue(ctl.start("w")["ok"])
        self.assertTrue(wait_for(lambda: ctl.status()["state"] == "connected"))


class TestStatus(SsBase):
    def assert_connected_without_counters(self):
        with mock.patch.object(ctl, "descendant_pids", return_value={4242}):
            st = ctl.status()
        self.assertEqual(st["state"], "connected")
        self.assertIsNone(st["error"])
        self.assertIsNone(st["rx_bytes"])
        self.assertIsNone(st["tx_bytes"])

    def test_counters_reported(self):
        self.connect()
        with mock.patch.object(ctl, "descendant_pids", return_value={4242}):
            st = ctl.status()
        self.assertEqual(st["state"], "connected")
        self.assertEqual(st["rx_bytes"], 35501)
        self.assertEqual(st["tx_bytes"], 52066)

    def test_missing_ss(self):
        self.connect()
        self.set_env(SS_BIN=os.path.join(self.run_dir.name, "nope"))
        self.assert_connected_without_counters()

    def test_ss_exits_nonzero(self):
        self.write_ss("exit 1\n")
        self.connect()
        self.assert_connected_without_counters()

    def test_ss_garbage(self):
        self.set_canned("hello\nworld\n")
        self.connect()
        self.assert_connected_without_counters()

    def test_ss_hangs(self):
        self.write_ss("exec sleep 5\n")
        self.connect()
        t = time.monotonic()
        self.assert_connected_without_counters()
        self.assertLess(time.monotonic() - t, 3)

    def test_disconnected_has_null_counters_and_no_ss_call(self):
        st = ctl.status()
        self.assertEqual(st["state"], "disconnected")
        self.assertIsNone(st["rx_bytes"])
        self.assertIsNone(st["tx_bytes"])
        self.assertFalse(os.path.exists(self.log))


class TestStatusConnecting(SsBase):
    mode = "silent"

    def test_connecting_has_null_counters_and_no_ss_call(self):
        self.assertTrue(ctl.start("w")["ok"])
        st = ctl.status()
        self.assertEqual(st["state"], "connecting")
        self.assertIsNone(st["rx_bytes"])
        self.assertIsNone(st["tx_bytes"])
        self.assertFalse(os.path.exists(self.log))


if __name__ == "__main__":
    unittest.main()
