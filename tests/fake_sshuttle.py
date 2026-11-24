#!/usr/bin/env python3
"""Stand-in for sshuttle used by the control tests. Behaviour via FAKE_MODE."""
import json
import os
import sys
import time

argv_file = os.environ.get("FAKE_ARGV_FILE")
if argv_file:
    with open(argv_file, "w") as f:
        json.dump(sys.argv, f)

mode = os.environ.get("FAKE_MODE", "ok")
if mode == "die":
    print("Permission denied (publickey).", file=sys.stderr, flush=True)
    sys.exit(255)
if mode == "ok":
    print("c : Connected to server.", file=sys.stderr, flush=True)
while True:
    time.sleep(1)
