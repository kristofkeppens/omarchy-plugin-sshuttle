import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ctl  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.dict(os.environ, {"SSHUTTLE_PANEL_CONFIG_DIR": self.tmp.name})
        p.start()
        self.addCleanup(p.stop)
        self.store = Path(self.tmp.name) / "profiles.json"


class TestValidation(Base):
    def test_defaults_filled(self):
        p = ctl.validate_profile({"name": "work", "remote": "me@host"})
        self.assertEqual(p["subnets"], ["0/0"])
        self.assertEqual(p["method"], "auto")
        self.assertIs(p["dns"], False)
        self.assertEqual(p["excludes"], [])
        self.assertEqual(p["ssh_identity"], "")

    def test_rejects_option_injection(self):
        base = {"name": "w", "remote": "h"}
        bad = [
            {"remote": "-oProxyCommand=x"}, {"remote": "a b"},
            {"remote": "host;rm"}, {"remote": ""},
            {"subnets": ["-x"]}, {"subnets": ["1.2.3.4/33"]}, {"subnets": ["abc"]},
            {"excludes": ["-x"]},
            {"method": "evil"}, {"name": ""}, {"name": "x" * 41},
            {"ssh_identity": "-oFoo=bar"}, {"ssh_identity": "/a b/key"},
            {"remote": "host\n"}, {"subnets": ["10.0.0.0/8\n"]},
            {"excludes": ["10.0.0.0/8\n"]},
            {"remote": "host:\u0661\u0662"}, {"subnets": ["\u0661\u0662.0.0.0/8"]},
            {"remote": "\u0661.\u0662.\u0663.\u0664"},
            {"ssh_identity": "/k'x"}, {"ssh_identity": '/k"x'}, {"ssh_identity": "/k\\x"},
            {"name": "  "}, {"name": "\t "},
            {"remote": "host:0"}, {"remote": "host:99999"},
            {"subnets": ["fd00::/129"]}, {"subnets": ["10.0.0.0/8:90-80"]},
            {"subnets": ["10.0.0.0/8:0"]}, {"subnets": ["10.0.0.0/8:70000"]},
        ]
        for override in bad:
            with self.subTest(override=override):
                with self.assertRaises(ctl.ProfileError):
                    ctl.validate_profile({**base, **override})

    def test_accepts_valid_forms(self):
        for r in ["host", "user@host:2222", "user@[::1]:22"]:
            with self.subTest(remote=r):
                ctl.validate_profile({"name": "w", "remote": r})
        for s in ["10.0.0.0/8", "0/0", "fd00::/8", "10.0.0.0/8:80-90"]:
            with self.subTest(subnet=s):
                ctl.validate_profile({"name": "w", "remote": "h", "subnets": [s]})


class TestArgv(Base):
    def test_build_argv_exact(self):
        prof = {"name": "w", "remote": "me@h:22", "subnets": ["10.0.0.0/8"],
                "excludes": ["10.1.0.0/16"], "dns": True, "method": "nft",
                "disable_ipv6": True, "ssh_identity": "/k"}
        self.assertEqual(ctl.build_argv(prof, "sshuttle"), [
            "sshuttle", "-r", "me@h:22", "--method", "nft", "--dns",
            "--disable-ipv6", "-x", "10.1.0.0/16", "-e",
            "ssh -o BatchMode=yes -o ConnectTimeout=15 -i /k", "10.0.0.0/8"])
        prof.update(method="auto", ssh_identity="")
        argv = ctl.build_argv(prof, "sshuttle")
        self.assertNotIn("--method", argv)
        self.assertNotIn("-i", argv[argv.index("-e") + 1].split())


class TestStore(Base):
    def test_load_store_corrupt(self):
        for content in [b"", b"{nope", b'{"profiles": 5}']:
            with self.subTest(content=content):
                self.store.write_bytes(content)
                self.assertEqual(ctl.load_store(), {"profiles": [], "default": None})
                self.assertEqual(self.store.read_bytes(), content)

    def test_load_store_missing(self):
        self.assertEqual(ctl.load_store(), {"profiles": [], "default": None})

    def test_save_replaces_by_name_and_delete_clears_default(self):
        ctl.save_profile({"name": "a", "remote": "h1"})
        ctl.save_profile({"name": "a", "remote": "h2"})
        st = ctl.load_store()
        self.assertEqual(len(st["profiles"]), 1)
        self.assertEqual(st["profiles"][0]["remote"], "h2")
        ctl.set_default("a")
        self.assertEqual(ctl.load_store()["default"], "a")
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o600)
        ctl.delete_profile("a")
        self.assertIsNone(ctl.load_store()["default"])
        with self.assertRaises(ctl.ProfileError):
            ctl.set_default("missing")


class TestCli(Base):
    def run_main(self, args, stdin=""):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin) if isinstance(stdin, str) else stdin), redirect_stdout(out):
            rc = ctl.main(args)
        return rc, json.loads(out.getvalue())

    def test_main_save_list_cli(self):
        rc, out = self.run_main(["save"], json.dumps({"name": "w", "remote": "h"}))
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        rc, out = self.run_main(["list"])
        self.assertEqual(rc, 0)
        self.assertEqual(out["profiles"][0]["name"], "w")
        rc, out = self.run_main(["set-default", "w"])
        self.assertEqual((rc, out), (0, {"ok": True}))
        self.assertEqual(ctl.load_store()["default"], "w")
        rc, out = self.run_main(["set-default", "nope"])
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])
        rc, out = self.run_main(["delete", "w"])
        self.assertEqual((rc, out), (0, {"ok": True}))
        self.assertEqual(ctl.load_store(), {"profiles": [], "default": None})
        rc, out = self.run_main(["delete"])
        self.assertEqual(rc, 1)
        rc, out = self.run_main(["save"], "{nope")
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])
        self.assertIn("error", out)

    def test_length_limits(self):
        base = {"name": "a", "remote": "h"}
        for bad in ({"remote": "h" * 301}, {"subnets": ["10.0.0.0/8"] * 201},
                    {"subnets": ["1" * 101]}, {"ssh_identity": "/" + "k" * 4096}):
            with self.assertRaises(ctl.ProfileError):
                ctl.validate_profile({**base, **bad})
        ctl.validate_profile({**base, "ssh_identity": "/" + "k" * 4095, "subnets": ["10.0.0.0/8"] * 200})

    def test_save_json_arg_never_reads_stdin(self):
        class Boom:
            def read(self):
                raise AssertionError("stdin must not be read")
        rc, out = self.run_main(["save", "--json", json.dumps({"name": "a", "remote": "h"})], Boom())
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        self.assertEqual(ctl.load_store()["profiles"][0]["name"], "a")

    def test_save_json_arg_rejects_bad_input(self):
        class Boom:
            def read(self):
                raise AssertionError("stdin must not be read")
        for bad in (["save", "--json", "{nope"],
                    ["save", "--json"],
                    ["save", "--json", json.dumps({"name": "a", "remote": "h" * (16 * 1024)})]):
            rc, out = self.run_main(bad, Boom())
            self.assertEqual(rc, 1, bad[:2])
            self.assertFalse(out["ok"])
            self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()


class ExcludeHostnames(Base):
    base = {"name": "w", "remote": "h"}

    def test_excludes_accept_hostnames_and_ranges(self):
        good = [
            "intranet.example.com", "example.com:443", "example.com:8000-8080",
            "localhost", "cafe.be", "a-b.example.org", "10.1.0.0/16", "fd00::/8",
            "10.0.0.5",
        ]
        p = ctl.validate_profile({**self.base, "excludes": good})
        self.assertEqual(p["excludes"], good)

    def test_excludes_reject_bad_hostnames(self):
        bad = [
            "-evil.example.com", "-x", "exa mple.com", "example.com/24",
            "example..com", ".example.com", "example.com.", "ex_ample.com",
            "example.com:0", "example.com:70000", "example.com:90-80",
            "example.com:abc", "exa$mple.com", "ex\nample.com", "example.com\n",
            "10.0.0.256", "300.1.1.1", "999.999.999.999", "*.example.com",
            "http://example.com", "user@example.com", "a" * 64 + ".com",
        ]
        for v in bad:
            with self.subTest(exclude=v):
                with self.assertRaises(ctl.ProfileError):
                    ctl.validate_profile({**self.base, "excludes": [v]})

    def test_subnets_still_reject_hostnames(self):
        # Only excludes take hostnames; the include list stays addresses/ranges.
        for v in ["example.com", "intranet.example.com:443"]:
            with self.subTest(subnet=v):
                with self.assertRaises(ctl.ProfileError):
                    ctl.validate_profile({**self.base, "subnets": [v]})

    def test_argv_passes_hostname_exclude_as_one_element(self):
        p = ctl.validate_profile({**self.base, "excludes": ["example.com:443", "10.1.0.0/16"]})
        argv = ctl.build_argv(p, "sshuttle")
        self.assertEqual(argv.count("-x"), 2)
        self.assertEqual(argv[argv.index("-x") + 1], "example.com:443")
        self.assertIn("10.1.0.0/16", argv)
