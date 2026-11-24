#!/usr/bin/env python3
"""Helper for the Omarchy sshuttle panel. Stdlib only.

Stdout is always one JSON object; exit code 0 when ok, 1 otherwise.
"""
import fcntl
import ipaddress
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
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
_HOST = rf"(?:{_LABEL}(?:\.{_LABEL})*|[0-9]{{1,3}}(?:\.[0-9]{{1,3}}){{3}}|\[[0-9A-Fa-f:.]+\])"
_REMOTE_RE = re.compile(rf"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?({_HOST})(?::([0-9]{{1,5}}))?")
_SUBNET_RE = re.compile(
    r"(?P<addr>[0-9A-Fa-f:.]+)(?:/(?P<mask>[0-9]{1,3}))?"
    r"(?::(?P<p1>[0-9]{1,5})(?:-(?P<p2>[0-9]{1,5}))?)?"
)
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _check_port(p, what):
    if p is not None and not 0 < int(p) <= 65535:
        raise ProfileError(f"invalid port in {what}")


def _validate_remote(v):
    if not isinstance(v, str) or len(v) > 300 or not (m := _REMOTE_RE.fullmatch(v)):
        raise ProfileError("invalid remote; expected [user@]host[:port]")
    host = m.group(1)
    if host.startswith("["):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            raise ProfileError("invalid IPv6 address in remote")
    elif re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,3}){3}", host):
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            raise ProfileError("invalid IPv4 address in remote")
    _check_port(m.group(2), "remote")
    return v


def _validate_subnet(v):
    if not isinstance(v, str) or not (m := _SUBNET_RE.fullmatch(v)):
        raise ProfileError(f"invalid subnet: {v!r}")
    addr, mask = m.group("addr"), m.group("mask")
    try:
        if ":" in addr:
            ipaddress.IPv6Address(addr)
            maxmask = 128
        else:
            # allow shorthand like "0" (as in "0/0")
            if not re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,3}){0,3}", addr):
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
    if len(v) > 200 or any(isinstance(x, str) and len(x) > 100 for x in v):
        raise ProfileError(f"{key} has too many or too long entries")
    return [check(x) for x in v]


def _validate_identity(v):
    if not isinstance(v, str):
        raise ProfileError("ssh_identity must be a string")
    if len(v) > 4096:
        raise ProfileError("ssh_identity too long")
    if v and (v.startswith("-") or re.search(r"[\s'\"\\]", v) or _CTRL_RE.search(v)):
        raise ProfileError("invalid ssh_identity path")
    return v


def validate_profile(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ProfileError("profile must be an object")
    name = raw.get("name")
    if not isinstance(name, str) or not 1 <= len(name) <= 40 or not name.strip() or _CTRL_RE.search(name):
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
# Process control
# --------------------------------------------------------------------------

CONNECTED_MARKER = "Connected to server."
_children = {}  # pid -> Popen, so exited children are reaped in-process


def _bin() -> str:
    return os.environ.get("SSHUTTLE_BIN") or "sshuttle"


def _installed() -> bool:
    return shutil.which(_bin()) is not None


def _not_found_msg() -> str:
    return f"sshuttle not found ({_bin()}); install it first"


def _connect_timeout() -> float:
    try:
        return float(os.environ.get("SSHUTTLE_PANEL_CONNECT_TIMEOUT", "30"))
    except ValueError:
        return 30.0


def _state_path() -> Path:
    return runtime_dir() / "state.json"


def _log_path() -> Path:
    return runtime_dir() / "sshuttle.log"


def _read_state():
    try:
        st = json.loads(_state_path().read_text())
        if isinstance(st, dict) and type(st.get("pid")) is int and st["pid"] > 1:
            return st
    except (OSError, ValueError):
        pass
    return None


def _write_state(st):
    d = runtime_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(st, f)
        os.replace(tmp, _state_path())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _remove_state():
    try:
        _state_path().unlink()
    except FileNotFoundError:
        pass


def _pid_state(pid: int) -> str:
    """'gone', 'ours' (our sshuttle) or 'foreign' (pid exists, other program)."""
    child = _children.get(pid)
    if child is not None and child.poll() is not None:
        del _children[pid]
        return "gone"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "gone"
    except PermissionError:
        pass  # exists, owned by someone else; the cmdline check decides
    cmdline = b""
    for attempt in range(5):
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        except FileNotFoundError:
            return "gone"
        except OSError:
            return "foreign"
        if cmdline:
            break
        time.sleep(0.02)  # empty mid-exec; stays empty for a zombie
    else:
        return "gone"
    # sshuttle is a python script: argv is `python3 /usr/bin/sshuttle ...`
    argv = [a for a in cmdline.split(b"\0") if a]
    # right after fork the shebang's `env` has not exec'd python yet:
    # `/usr/bin/env python3 /usr/bin/sshuttle ...`
    argv = argv[1:4] if argv[:1] and os.path.basename(argv[0]) == b"env" else argv[:2]
    want = os.path.basename(_bin()).encode()
    return "ours" if any(os.path.basename(a) == want for a in argv) else "foreign"


def _alive(pid: int) -> bool:
    """True if pid exists and is (still) our sshuttle, not a reused pid."""
    return _pid_state(pid) == "ours"


@contextmanager
def _lock():
    d = runtime_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(d / "lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield  # released when f closes


def _signal(pid: int, sig) -> None:
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if not _alive(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _terminate(pid: int) -> None:
    _signal(pid, signal.SIGTERM)
    if not _wait_gone(pid, 5.0):
        _signal(pid, signal.SIGKILL)
        _wait_gone(pid, 2.0)
    child = _children.pop(pid, None)
    if child is not None:
        try:
            child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


def read_log() -> list:
    try:
        with open(_log_path(), "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    return text.splitlines()[-50:]


def _last_log_line():
    for line in reversed(read_log()):
        if line.strip():
            return line.strip()
    return None


def start(name) -> dict:
    with _lock():
        return _start(name)


def _start(name) -> dict:
    if not _installed():
        raise ProfileError(_not_found_msg())
    if status()["state"] in ("connecting", "connected"):
        raise ProfileError("already running")
    store = load_store()
    name = name or store["default"]
    if not name:
        raise ProfileError("no profile given and no default profile set")
    profile = next((p for p in store["profiles"] if p["name"] == name), None)
    if profile is None:
        raise ProfileError(f"unknown profile: {name}")
    _remove_state()
    argv = build_argv(profile, _bin())
    d = runtime_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(_log_path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(_log_path(), 0o600)
    with os.fdopen(fd, "wb") as log:
        try:
            proc = subprocess.Popen(
                argv, start_new_session=True, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT)
        except OSError as e:
            _clear_sudo_cache()
            raise ProfileError(f"could not start sshuttle: {e}")
    _children[proc.pid] = proc
    _write_state({"pid": proc.pid, "profile": name, "started": time.time()})
    return {"ok": True, "pid": proc.pid}


def stop() -> dict:
    with _lock():
        return _stop()


def _stop() -> dict:
    st = _read_state()
    if st is not None and _alive(st["pid"]):
        _terminate(st["pid"])
    _remove_state()
    return {"ok": True}


def _record_error(st, msg):
    """Make a failure sticky (until stop/start) and drop the cached sudo
    readiness: a failed connect is often sudo asking for a password."""
    _write_state({**st, "error": msg})
    _clear_sudo_cache()


def status() -> dict:
    out = {"ok": True, "state": "disconnected", "profile": None, "pid": None,
           "since": None, "error": None,
           "sshuttle_installed": _installed(), "sudo_ready": False}
    out["sudo_ready"] = out["sshuttle_installed"] and sudo_ready(_bin())
    st = _read_state()
    if st is None:
        if not out["sshuttle_installed"]:
            out.update(state="error", error=_not_found_msg())
        return out
    pid = st["pid"]
    started = st.get("started")
    out["profile"] = st.get("profile")
    out["since"] = float(started) if isinstance(started, (int, float)) else None
    if isinstance(st.get("error"), str):  # a recorded failure persists until stop/start
        out.update(state="error", error=st["error"])
        return out
    pst = _pid_state(pid)
    if pst != "ours":
        # An error is only reported when the process died after a failed
        # start. Reached `connected` and gone (clean kill), or a foreign
        # process owns the pid -> just disconnected.
        had_connected = bool(st.get("connected")) or any(
            CONNECTED_MARKER in l for l in read_log())
        msg = None
        if pst == "gone" and not had_connected and st.get("profile") and _log_path().exists():
            msg = _last_log_line()
        if msg is None:
            _remove_state()
            out.update(profile=None, since=None)
            if not out["sshuttle_installed"]:
                out.update(state="error", error=_not_found_msg())
        else:
            _record_error(st, msg)
            out.update(state="error", error=msg)
        return out
    out["pid"] = pid
    if st.get("connected") or any(CONNECTED_MARKER in l for l in read_log()):
        if not st.get("connected"):
            _write_state({**st, "connected": True})
        out["state"] = "connected"
        return out
    out["state"] = "connecting"
    if isinstance(started, (int, float)) and time.time() - started > _connect_timeout():
        _terminate(pid)
        msg = f"connection timed out after {_connect_timeout():g}s"
        _record_error(st, msg)
        out.update(state="error", error=msg, pid=None)
    return out


# --------------------------------------------------------------------------
# Passwordless sudo
# --------------------------------------------------------------------------

SUDOERS_TARGET = "/etc/sudoers.d/sshuttle-panel"


def _tool(env_name, default):
    return os.environ.get(env_name) or default


PROBE_TIMEOUT = 3.0

_ALIAS_RE = re.compile(
    r"Cmnd_Alias ([A-Z][A-Z0-9_]*) = /usr/bin/env PYTHONPATH=([A-Za-z0-9_./+-]+) "
    r"([A-Za-z0-9_./+-]+) \*")


def _current_user() -> str:
    import pwd
    return pwd.getpwuid(os.getuid()).pw_name


def parse_rule(text: str, user: str, sshuttle_path: str) -> list:
    """Strictly validate sshuttle's sudoers text and return the command argv
    (without the trailing `*`). Raises ProfileError on anything unexpected."""
    if any(ord(c) < 32 and c != "\n" or ord(c) == 127 for c in text):
        raise ProfileError("unexpected sudoers rule: control characters")
    alias = cmd = None
    rule_seen = False
    for line in text.split("\n"):
        if not line.strip():
            continue
        if line.startswith("#"):
            if line.startswith("#include"):
                raise ProfileError("unexpected sudoers rule: include directive")
            continue
        if alias is None and (m := _ALIAS_RE.fullmatch(line)):
            same = m.group(3) == sshuttle_path or \
                os.path.realpath(m.group(3)) == os.path.realpath(sshuttle_path)
            if not same:
                raise ProfileError("unexpected sudoers rule: command is not the resolved sshuttle")
            alias = m.group(1)
            cmd = ["/usr/bin/env", f"PYTHONPATH={m.group(2)}", m.group(3)]
        elif alias is not None and not rule_seen and line == f"{user} ALL=NOPASSWD: {alias}":
            rule_seen = True
        else:
            raise ProfileError(f"unexpected sudoers rule line: {line[:60]!r}")
    if alias is None or not rule_seen:
        raise ProfileError("unexpected sudoers rule: missing alias or user rule")
    return cmd


def _generate_rule(timeout: float) -> str:
    user = _current_user()
    gen = subprocess.run(
        [_bin(), "--sudoers-no-modify", "--sudoers-user", user],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    if gen.returncode != 0 or not gen.stdout.strip():
        raise ProfileError("could not generate sudoers rule: "
                           + (gen.stderr.strip().splitlines() or ["no output"])[-1])
    return gen.stdout if gen.stdout.endswith("\n") else gen.stdout + "\n"


# `sudo -l CMD` only says the command is permitted, which the blanket %wheel
# rule answers yes to for everything (and listpw=any lets -n -l pass without a
# password). The long listing prints the matched entry's tags instead; a
# NOPASSWD entry shows `!authenticate` on its Options line (sudoers display.c:
# "    Options: " followed by "%sauthenticate, "). Same check as omarchy-dns.
_NOPASSWD_RE = re.compile(r"^[ \t]*Options:(?:[^\n]*?[ \t,])?!authenticate(?=,|[ \t]|$)", re.M)


def _probe_sudo(path: str) -> bool:
    try:
        cmd = parse_rule(_generate_rule(PROBE_TIMEOUT), _current_user(), path)
        r = subprocess.run(
            [_tool("SUDO_BIN", "sudo"), "-n", "-l", "-l", *cmd, "--firewall"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            errors="replace", timeout=PROBE_TIMEOUT)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and _NOPASSWD_RE.search(r.stdout or "") is not None


def _sudo_ttl() -> float:
    try:
        return float(os.environ.get("SSHUTTLE_PANEL_SUDO_TTL", "60"))
    except ValueError:
        return 60.0


def _sudo_cache_path() -> Path:
    return runtime_dir() / "sudo.json"


def _read_sudo_cache(path: str):
    """The cached bool for this sshuttle path, or None if absent/stale/bad."""
    try:
        data = json.loads(_sudo_cache_path().read_text())
        ts, val = data["ts"], data["value"]
        if data["path"] != path or type(val) is not bool or type(ts) not in (int, float):
            return None
        age = time.time() - ts
        return val if 0 <= age < _sudo_ttl() else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_sudo_cache(path: str, val: bool) -> None:
    try:
        d = runtime_dir()
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".sudo-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"path": path, "ts": time.time(), "value": val}, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, _sudo_cache_path())
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        pass  # caching is best effort


def _clear_sudo_cache() -> None:
    try:
        _sudo_cache_path().unlink()
    except OSError:
        pass


def sudo_ready(sshuttle_bin: str) -> bool:
    """True iff sudo would run sshuttle's firewall helper without a password.
    Status polls each run a fresh process, so the result (True or False) is
    cached on disk per sshuttle path (SSHUTTLE_PANEL_SUDO_TTL, default 60s)."""
    try:
        path = shutil.which(sshuttle_bin)
        if path is None:
            return False
        hit = _read_sudo_cache(path)
        if hit is not None:
            return hit
        val = _probe_sudo(path)
        _write_sudo_cache(path, val)
        return val
    except Exception:
        return False


def _fail(msg):
    return {"ok": False, "error": msg}


def setup_sudo() -> dict:
    """Install a sudoers rule for sshuttle via pkexec (the only privileged step)."""
    if not _installed():
        return _fail(_not_found_msg())
    try:
        rule = _generate_rule(30)
        parse_rule(rule, _current_user(), shutil.which(_bin()))
    except ProfileError as e:
        return _fail(str(e))
    except (OSError, subprocess.SubprocessError, KeyError) as e:
        return _fail(f"could not generate sudoers rule: {e}")
    data = rule.encode()  # exactly these bytes are validated and installed
    # Validate on a private copy (0700 dir, 0600 file). The privileged step
    # never reads a file: the same bytes go to `install` on stdin.
    try:
        tmpdir = tempfile.mkdtemp(prefix="sshuttle-panel-")
    except OSError as e:
        return _fail(f"could not create temporary file: {e}")
    try:
        tmp = os.path.join(tmpdir, "sudoers")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except OSError as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return _fail(f"could not create temporary file: {e}")
    try:
        try:
            chk = subprocess.run(
                [_tool("VISUDO_BIN", "visudo"), "-c", "-f", tmp],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as e:
            return _fail(f"sudoers validation could not run: {e}")
        if chk.returncode != 0:
            detail = (chk.stderr.strip() or chk.stdout.strip()).splitlines()
            return _fail("sudoers validation failed" + (f": {detail[-1]}" if detail else ""))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    try:
        inst = subprocess.run(
            [_tool("PKEXEC_BIN", "pkexec"), "/usr/bin/install", "-m", "0440",
             "/dev/stdin", SUDOERS_TARGET],
            input=data, capture_output=True)
    except OSError as e:
        return _fail(f"could not run pkexec: {e}")
    finally:
        _clear_sudo_cache()  # whatever happened, the next status re-probes
    if inst.returncode == 126:
        return _fail("Authorization cancelled")
    if inst.returncode != 0:
        detail = inst.stderr.decode("utf-8", "replace").strip().splitlines()
        return _fail("install failed" + (f": {detail[-1]}" if detail else ""))
    return {"ok": True, "sudo_ready": sudo_ready(_bin())}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

MAX_JSON_ARG = 16 * 1024


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
        if args and args[0] == "--json":
            if len(args) != 2:
                raise ProfileError("--json expects exactly one argument")
            text, where = args[1], "argument"
            if len(text.encode("utf-8")) > MAX_JSON_ARG:
                raise ProfileError("profile JSON too large")
        elif args:
            raise ProfileError("unexpected arguments for save")
        else:
            text, where = sys.stdin.read(), "stdin"
        try:
            raw = json.loads(text)
        except ValueError:
            raise ProfileError("invalid JSON on " + where)
        return {"ok": True, "profile": save_profile(raw)}
    if cmd == "delete":
        delete_profile(_need_name(args))
        return {"ok": True}
    if cmd == "set-default":
        set_default(_need_name(args))
        return {"ok": True}
    if cmd == "start":
        if len(args) > 1:
            raise ProfileError("expected at most one NAME argument")
        return start(args[0] if args else None)
    if cmd == "stop":
        return stop()
    if cmd == "status":
        return status()
    if cmd == "setup-sudo":
        res = setup_sudo()
        if not res["ok"]:
            raise ProfileError(res["error"])
        return res
    if cmd == "log":
        return {"ok": True, "lines": read_log()}
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
