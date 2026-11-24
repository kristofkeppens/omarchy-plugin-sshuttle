#!/usr/bin/env python3
"""Helper for the Omarchy sshuttle panel. Stdlib only.

Stdout is always one JSON object; exit code 0 when ok, 1 otherwise.
"""
import ipaddress
import json
import os
import re
import sys
import tempfile
from pathlib import Path

METHODS = ("auto", "nft", "nat", "tproxy")
SSH_BASE_OPTS = "ssh -o BatchMode=yes -o ConnectTimeout=15"


class ProfileError(ValueError):
    pass


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def config_dir() -> Path:
    env = os.environ.get("SSHUTTLE_PANEL_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".config" / "sshuttle-panel"


def runtime_dir() -> Path:
    env = os.environ.get("SSHUTTLE_PANEL_RUNTIME_DIR")
    if env:
        return Path(env)
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "sshuttle-panel"


def _store_path() -> Path:
    return config_dir() / "profiles.json"


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST = rf"(?:{_LABEL}(?:\.{_LABEL})*|\d{{1,3}}(?:\.\d{{1,3}}){{3}}|\[[0-9A-Fa-f:.]+\])"
_REMOTE_RE = re.compile(rf"^(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?({_HOST})(?::(\d{{1,5}}))?$")
_SUBNET_RE = re.compile(
    r"^(?P<addr>[0-9A-Fa-f:.]+)(?:/(?P<mask>\d{1,3}))?"
    r"(?::(?P<p1>\d{1,5})(?:-(?P<p2>\d{1,5}))?)?$"
)
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _check_port(p, what):
    if p is not None and not 0 < int(p) <= 65535:
        raise ProfileError(f"invalid port in {what}")


def _validate_remote(v):
    if not isinstance(v, str) or not (m := _REMOTE_RE.match(v)):
        raise ProfileError("invalid remote; expected [user@]host[:port]")
    host = m.group(1)
    if host.startswith("["):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            raise ProfileError("invalid IPv6 address in remote")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host):
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            raise ProfileError("invalid IPv4 address in remote")
    _check_port(m.group(2), "remote")
    return v


def _validate_subnet(v):
    if not isinstance(v, str) or not (m := _SUBNET_RE.match(v)):
        raise ProfileError(f"invalid subnet: {v!r}")
    addr, mask = m.group("addr"), m.group("mask")
    try:
        if ":" in addr:
            ipaddress.IPv6Address(addr)
            maxmask = 128
        else:
            # allow shorthand like "0" (as in "0/0")
            if not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){0,3}", addr):
                raise ValueError
            parts = addr.split(".")
            if any(int(p) > 255 for p in parts):
                raise ValueError
            maxmask = 32
    except ValueError:
        raise ProfileError(f"invalid subnet address: {v!r}")
    if mask is not None and int(mask) > maxmask:
        raise ProfileError(f"invalid subnet mask: {v!r}")
    _check_port(m.group("p1"), "subnet")
    _check_port(m.group("p2"), "subnet")
    if m.group("p2") and int(m.group("p1")) > int(m.group("p2")):
        raise ProfileError(f"invalid port range: {v!r}")
    return v


def _validate_list(raw, key, default, check):
    v = raw.get(key, default)
    if not isinstance(v, list):
        raise ProfileError(f"{key} must be a list")
    return [check(x) for x in v]


def _validate_identity(v):
    if not isinstance(v, str):
        raise ProfileError("ssh_identity must be a string")
    if v and (v.startswith("-") or re.search(r"\s", v) or _CTRL_RE.search(v)):
        raise ProfileError("invalid ssh_identity path")
    return v


def validate_profile(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ProfileError("profile must be an object")
    name = raw.get("name")
    if not isinstance(name, str) or not 1 <= len(name) <= 40 or _CTRL_RE.search(name):
        raise ProfileError("name must be 1-40 characters without control characters")
    method = raw.get("method", "auto")
    if method not in METHODS:
        raise ProfileError("invalid method")
    for key in ("dns", "disable_ipv6"):
        if not isinstance(raw.get(key, False), bool):
            raise ProfileError(f"{key} must be a boolean")
    return {
        "name": name,
        "remote": _validate_remote(raw.get("remote")),
        "subnets": _validate_list(raw, "subnets", ["0/0"], _validate_subnet) or ["0/0"],
        "excludes": _validate_list(raw, "excludes", [], _validate_subnet),
        "dns": raw.get("dns", False),
        "method": method,
        "disable_ipv6": raw.get("disable_ipv6", False),
        "ssh_identity": _validate_identity(raw.get("ssh_identity", "")),
    }


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

def _empty_store():
    return {"profiles": [], "default": None}


def load_store() -> dict:
    try:
        data = json.loads(_store_path().read_text())
        profiles = data["profiles"]
        if not isinstance(profiles, list):
            return _empty_store()
        clean = []
        for p in profiles:
            try:
                clean.append(validate_profile(p))
            except ProfileError:
                continue
        default = data.get("default")
        if default not in [p["name"] for p in clean]:
            default = None
        return {"profiles": clean, "default": default}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return _empty_store()


def _write_store(store):
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".profiles-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(store, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, _store_path())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_profile(raw) -> dict:
    prof = validate_profile(raw)
    store = load_store()
    for i, p in enumerate(store["profiles"]):
        if p["name"] == prof["name"]:
            store["profiles"][i] = prof
            break
    else:
        store["profiles"].append(prof)
    _write_store(store)
    return prof


def delete_profile(name: str) -> None:
    store = load_store()
    store["profiles"] = [p for p in store["profiles"] if p["name"] != name]
    if store["default"] == name:
        store["default"] = None
    _write_store(store)


def set_default(name: str) -> None:
    store = load_store()
    if name not in [p["name"] for p in store["profiles"]]:
        raise ProfileError(f"unknown profile: {name}")
    store["default"] = name
    _write_store(store)


# --------------------------------------------------------------------------
# sshuttle argv
# --------------------------------------------------------------------------

def build_argv(profile: dict, sshuttle_bin: str) -> list:
    argv = [sshuttle_bin, "-r", profile["remote"]]
    if profile.get("method", "auto") != "auto":
        argv += ["--method", profile["method"]]
    if profile.get("dns"):
        argv.append("--dns")
    if profile.get("disable_ipv6"):
        argv.append("--disable-ipv6")
    for x in profile.get("excludes", []):
        argv += ["-x", x]
    ssh = SSH_BASE_OPTS
    if profile.get("ssh_identity"):
        ssh += f" -i {profile['ssh_identity']}"
    argv += ["-e", ssh]
    argv += list(profile.get("subnets") or ["0/0"])
    return argv


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")


def _need_name(args):
    if len(args) != 1:
        raise ProfileError("expected exactly one NAME argument")
    return args[0]


def _dispatch(cmd, args):
    if cmd == "list":
        return {"ok": True, **load_store()}
    if cmd == "save":
        try:
            raw = json.loads(sys.stdin.read())
        except ValueError:
            raise ProfileError("invalid JSON on stdin")
        return {"ok": True, "profile": save_profile(raw)}
    if cmd == "delete":
        delete_profile(_need_name(args))
        return {"ok": True}
    if cmd == "set-default":
        set_default(_need_name(args))
        return {"ok": True}
    raise ProfileError(f"unknown command: {cmd}")


def main(argv: list) -> int:
    try:
        if not argv:
            raise ProfileError("missing command")
        result = _dispatch(argv[0], argv[1:])
    except (ProfileError, OSError) as e:
        _emit({"ok": False, "error": str(e)})
        return 1
    _emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
