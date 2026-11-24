"""Static checks over the plugin's QML, manifest, translations and installer.

These cannot replace running the panel, but they hold the safety rules that a
reviewer would otherwise have to check by eye:
  * every QML `Text` renders as plain text (profile names and helper output
    are untrusted; AutoText would interpret markup such as <img src=...>);
  * every animation duration goes through Style.duration() and stays < 300ms;
  * every translation key used by the QML exists in i18n/en.json;
  * the manifest matches the plugin contract.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "manifest.json"
EN = ROOT / "i18n" / "en.json"
INSTALL = ROOT / "install.sh"


def qml_files():
    return sorted(p for p in ROOT.glob("*.qml"))


def strip_comments_and_strings(src):
    """Blank out // and /* */ comments and string literals, keeping offsets
    (and newlines) so line numbers and brace positions stay valid."""
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif c in "\"'`":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            for k in range(i + 1, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j + 1
        else:
            i += 1
    return "".join(out)


def text_blocks(src):
    """Yield (line, top_level_body) for every `Text {` / `Text{` block. The
    body keeps only the block's own depth, so a nested child cannot satisfy
    the parent's check."""
    clean = strip_comments_and_strings(src)
    for m in re.finditer(r"\bText\s*\{", clean):
        start = m.end()
        depth, i = 1, start
        own = []
        while i < len(clean) and depth > 0:
            ch = clean[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            elif depth == 1:
                own.append(src[i] if clean[i] != " " or src[i] in " \n\t" else clean[i])
            i += 1
        line = clean.count("\n", 0, m.start()) + 1
        yield line, "".join(own)


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.m = json.loads(MANIFEST.read_text())

    def test_identity(self):
        m = self.m
        self.assertEqual(m["schemaVersion"], 1)
        self.assertEqual(m["id"], "sshuttle.panel")
        self.assertEqual(m["kinds"], ["bar-widget"])
        self.assertEqual(m["entryPoints"], {"barWidget": "Panel.qml"})
        self.assertTrue((ROOT / m["entryPoints"]["barWidget"]).is_file())
        bw = m["barWidget"]
        self.assertEqual(bw["defaultSection"], "right")
        self.assertEqual(bw["category"], "Network")
        self.assertIs(bw["allowMultiple"], False)

    def test_settings_schema(self):
        bw = self.m["barWidget"]
        expected = {"iconSize": 18, "refreshIntervalSec": 5, "showLabel": False}
        self.assertEqual(bw["defaults"], expected)
        schema = {s["key"]: s for s in bw["schema"]}
        self.assertEqual(set(schema), set(expected))
        for key, value in expected.items():
            self.assertEqual(schema[key]["defaultValue"], value, key)
            self.assertIsInstance(schema[key].get("label"), str, key)
        self.assertEqual(schema["iconSize"]["type"], "integer")
        self.assertEqual((schema["iconSize"]["min"], schema["iconSize"]["max"]), (8, 24))
        self.assertEqual(schema["refreshIntervalSec"]["type"], "integer")
        self.assertEqual((schema["refreshIntervalSec"]["min"], schema["refreshIntervalSec"]["max"]), (2, 60))
        self.assertEqual(schema["showLabel"]["type"], "boolean")


class QmlTextTest(unittest.TestCase):
    def test_there_is_qml_to_check(self):
        names = {p.name for p in qml_files()}
        self.assertIn("Panel.qml", names)
        self.assertIn("Service.qml", names)

    def test_every_text_is_plain(self):
        bad = []
        for path in qml_files():
            for line, body in text_blocks(path.read_text()):
                if not re.search(r"\btextFormat\s*:\s*Text\.PlainText\b", body):
                    bad.append(f"{path.name}:{line}")
        self.assertEqual(bad, [], "Text without textFormat: Text.PlainText")

    def test_checker_catches_missing_plaintext(self):
        sample = 'Item {\n  Text {\n    text: "a"\n    Rectangle { Text { textFormat: Text.PlainText } }\n  }\n}\n'
        results = list(text_blocks(sample))
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0][0], 2)
        self.assertNotIn("PlainText", results[0][1])  # nested child does not count
        self.assertIn("PlainText", results[1][1])

    def test_checker_ignores_braces_in_strings_and_comments(self):
        sample = 'Text { // }\n  text: "}"\n  textFormat: Text.PlainText\n}\n'
        (_, body), = list(text_blocks(sample))
        self.assertIn("textFormat", body)


class QmlMotionTest(unittest.TestCase):
    def test_durations_go_through_style(self):
        bad = []
        for path in qml_files():
            clean = strip_comments_and_strings(path.read_text())
            for no, line in enumerate(clean.splitlines(), 1):
                for m in re.finditer(r"\bduration\s*:\s*([^;\n}]*)", line):
                    if "Style.duration(" not in m.group(1):
                        bad.append(f"{path.name}:{no}: {m.group(0).strip()}")
        self.assertEqual(bad, [], "duration must use Style.duration(ms)")

    def test_durations_are_short(self):
        bad = []
        for path in qml_files():
            src = path.read_text()
            for m in re.finditer(r"Style\.duration\(\s*([0-9.]+)\s*\)", src):
                if float(m.group(1)) >= 300:
                    line = src.count("\n", 0, m.start()) + 1
                    bad.append(f"{path.name}:{line}: {m.group(0)}")
        self.assertEqual(bad, [], "animations must stay under 300ms")


class TranslationTest(unittest.TestCase):
    def setUp(self):
        self.en = json.loads(EN.read_text())

    def test_catalog_is_flat(self):
        for key, value in self.en.items():
            self.assertTrue(key.startswith("sshuttle."), key)
            if isinstance(value, dict):
                self.assertIn("one", value, key)
                self.assertIn("other", value, key)
            else:
                self.assertIsInstance(value, str, key)
                self.assertNotEqual(value, "", key)

    def test_used_keys_exist(self):
        missing = []
        for path in qml_files():
            src = path.read_text()
            for m in re.finditer(r"""strings\.(t|tn)\(\s*["']([^"']+)["']""", src):
                fn, key = m.groups()
                line = src.count("\n", 0, m.start()) + 1
                if key not in self.en:
                    missing.append(f"{path.name}:{line}: {key}")
                elif fn == "tn" and not isinstance(self.en[key], dict):
                    missing.append(f"{path.name}:{line}: {key} is not a plural entry")
        self.assertEqual(missing, [])

    def test_panel_uses_translations(self):
        src = (ROOT / "Panel.qml").read_text()
        self.assertRegex(src, r"Translations\s*\{[^}]*directory:\s*Qt\.resolvedUrl\(\"i18n\"\)")


def qml_function(src, name):
    """Body of `function NAME(...) { ... }` (comments/strings blanked for
    brace matching, original text returned)."""
    clean = strip_comments_and_strings(src)
    m = re.search(rf"\bfunction\s+{name}\s*\([^)]*\)\s*\{{", clean)
    if not m:
        return None
    depth, i = 1, m.end()
    while depth and i < len(clean):
        depth += {"{": 1, "}": -1}.get(clean[i], 0)
        i += 1
    return src[m.end():i - 1]


class PanelSafetyTest(unittest.TestCase):
    """Behaviour the panel must keep; QML cannot run here, so these hold the
    shape of the code."""

    def setUp(self):
        self.src = (ROOT / "Panel.qml").read_text()
        self.clean = strip_comments_and_strings(self.src)
        self.en = json.loads(EN.read_text())

    def test_setup_sudo_only_runs_after_confirmation(self):
        # The one call site is the confirmation handler.
        self.assertEqual(len(re.findall(r"svc\.setupSudo\(", self.clean)), 1)
        body = qml_function(self.src, "confirmAction")
        self.assertIsNotNone(body)
        self.assertIn("svc.setupSudo()", body)
        ask = qml_function(self.src, "askSetupSudo")
        self.assertIsNotNone(ask)
        self.assertNotIn("setupSudo", ask)
        self.assertRegex(self.src, r'strings\.t\("sshuttle\.sudoConfirm"\)')
        msg = self.en["sshuttle.sudoConfirm"].lower()
        for word in ("root", "password", "any program"):
            self.assertIn(word, msg)

    def test_confirm_dialog_starts_on_cancel(self):
        self.assertRegex(self.clean, r"onOpenedChanged:\s*if\s*\(opened\)\s*selectedIndex\s*=\s*0")

    def test_error_can_be_cleared(self):
        self.assertRegex(self.src, r'strings\.t\("sshuttle\.clearError"\)')
        body = qml_function(self.src, "clearError")
        self.assertIsNotNone(body)
        self.assertIn("svc.disconnect()", body)

    def test_switch_does_not_connect_without_sudo(self):
        body = qml_function(self.src, "togglePower")
        self.assertIn("needsSudo", body)
        m = re.search(r"ToggleSwitch\s*\{[^}]*?interactive:\s*([^\n]*)", self.clean)
        self.assertIsNotNone(m)
        self.assertIn("needsSudo", m.group(1))
        self.assertIn("isOn", m.group(1))  # disconnecting stays possible


# The only external tools install.sh may use. The test PATH holds nothing
# else, so the real omarchy-shell (in /usr/bin) can never be reached.
INSTALL_TOOLS = ["dirname", "mkdir", "ln", "readlink"]


class InstallScriptTest(unittest.TestCase):
    """install.sh runs against a temporary HOME with a PATH that holds only
    links to a few coreutils plus, optionally, a stub omarchy-shell, so the
    real shell is never touched."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        self.bare = Path(self.tmp.name) / "bare"
        self.bare.mkdir()
        for tool in INSTALL_TOOLS:
            for d in (self.bin, self.bare):
                (d / tool).symlink_to(shutil.which(tool))
        self.calls = Path(self.tmp.name) / "calls"
        stub = self.bin / "omarchy-shell"
        stub.write_text(f'#!/bin/sh\necho "$@" >> "{self.calls}"\n')
        stub.chmod(0o755)
        self.target = self.home / ".config" / "omarchy" / "plugins" / "sshuttle.panel"

    def tearDown(self):
        self.tmp.cleanup()

    def run_install(self, path_with_stub=True):
        env = {"HOME": str(self.home), "PATH": str(self.bin if path_with_stub else self.bare)}
        return subprocess.run([shutil.which("bash"), str(INSTALL)], env=env, cwd=self.tmp.name,
                              capture_output=True, text=True, timeout=30)

    def test_syntax(self):
        r = subprocess.run(["bash", "-n", str(INSTALL)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.stat(INSTALL).st_mode & stat.S_IXUSR, "install.sh must be executable")

    def test_creates_symlink_and_rescans(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(Path(os.readlink(self.target)), ROOT)
        self.assertEqual(self.calls.read_text().split("\n")[0], "shell rescanPlugins")

    def test_idempotent(self):
        self.assertEqual(self.run_install().returncode, 0)
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(Path(os.readlink(self.target)), ROOT)
        # No link was created inside the repo by a second `ln` into the link.
        self.assertFalse((ROOT / ROOT.name).exists())

    def test_replaces_stale_symlink(self):
        self.target.parent.mkdir(parents=True)
        self.target.symlink_to(self.tmp.name)
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(Path(os.readlink(self.target)), ROOT)

    def test_refuses_real_directory(self):
        self.target.mkdir(parents=True)
        (self.target / "keep").write_text("mine")
        r = self.run_install()
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.target.is_symlink())
        self.assertEqual((self.target / "keep").read_text(), "mine")
        self.assertEqual(sorted(p.name for p in self.target.iterdir()), ["keep"])
        self.assertIn("sshuttle.panel", r.stderr)

    def test_refuses_regular_file(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text("mine")
        r = self.run_install()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.target.read_text(), "mine")

    def test_without_omarchy_shell_still_links(self):
        r = self.run_install(path_with_stub=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.target.is_symlink())


if __name__ == "__main__":
    unittest.main()
