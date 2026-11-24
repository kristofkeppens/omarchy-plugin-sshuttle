#!/usr/bin/env python3
"""Stand-in for sshuttle used by the control tests. Behaviour via FAKE_MODE."""
import json
import os
import sys
import time

if "--sudoers-no-modify" in sys.argv:
    sys.exit(1)  # no rule; keeps status() probes instant

argv_file = os.environ.get("FAKE_ARGV_FILE")
if argv_file:
    with open(argv_file, "w") as f:
        json.dump(sys.argv, f)

env_file = os.environ.get("FAKE_ENV_FILE")
if env_file:
    with open(env_file, "w") as f:
        json.dump({"SSH_AUTH_SOCK": os.environ.get("SSH_AUTH_SOCK")}, f)

mode = os.environ.get("FAKE_MODE", "ok")
if mode == "die":
    print("Permission denied (publickey).", file=sys.stderr, flush=True)
    sys.exit(255)
if mode == "fwfail":  # handshake done, then the firewall step fails
    print("c : Connected to server.", file=sys.stderr, flush=True)
    time.sleep(0.2)
    print("c : fatal: ['nft', 'add', 'table'] returned 1", file=sys.stderr, flush=True)
    sys.exit(99)
if mode == "ok":
    print("c : Connected to server.", file=sys.stderr, flush=True)
while True:
    time.sleep(1)
