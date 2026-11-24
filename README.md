# omarchy-sshuttle-plugin

An Omarchy shell bar widget for [sshuttle](https://github.com/sshuttle/sshuttle).
Omarchy's bar is the Quickshell shell (`omarchy-shell`), not Waybar, so this is
a plugin with id `sshuttle.panel`. The bar icon shows whether sshuttle is
connected; clicking it opens a panel to manage profiles and connect or
disconnect.

Requirements: Omarchy with `omarchy-shell`, `sshuttle`, `python3`, `pkexec`
(polkit) and `visudo` (only for the sudo setup below).

## Install

```bash
./install.sh
```

This symlinks the checkout to `~/.config/omarchy/plugins/sshuttle.panel` and
asks the shell to rescan plugins (`omarchy-shell shell rescanPlugins`). It
refuses to replace a real directory or file at that path. Running it again is
safe.

## Add it to the bar

```bash
omarchy bar move sshuttle.panel --section right
```

or add an entry to the bar layout in `~/.config/omarchy/shell.json` (it
hot-reloads on save):

```json
{"id": "sshuttle.panel", "showLabel": true}
```

Valid sections, in the order they run along the bar: `left`, `centerBegin`,
`center`, `centerEnd`, `right`. To sit beside the clock use `centerBegin` or
`centerEnd`; putting it in `center` moves the clock off center. Moving it with
`omarchy bar move` again relocates it without a restart.

Settings (inline keys on the entry):

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `iconSize` | integer, 8-24 | 18 | Icon size |
| `refreshIntervalSec` | integer, 2-60 | 5 | Status refresh interval in seconds |
| `showLabel` | boolean | false | Show the active profile name next to the icon |

## Using the panel

The status is one of Connected, Connecting, Disconnected or Error. In the error
state the message (usually the last line of the sshuttle log) is shown and
stays until you disconnect or connect again. If sshuttle is not installed the
panel says so.

- The switch in the header connects or disconnects. It connects the selected
  profile (the default profile, else the first).
- Profiles are listed below; select one, mark one as default, add, edit or
  delete. Names must be unique; saving with an existing name replaces it.
- "Show log" displays the last 50 lines of sshuttle's output.
- If passwordless sudo is not set up the panel shows a "Set up" button, see
  below.

## Profile fields

| Field | Rules |
| --- | --- |
| Name | 1-40 characters, not blank, no control characters. |
| Remote | `[user@]host[:port]`. Host is a hostname, IPv4 address or `[IPv6]`. Port 1-65535. Max 300 characters. |
| Subnets | One per line, `addr[/mask][:port[-port]]`. Empty means `0/0` (all traffic). Max 200 entries of 100 characters. |
| Excluded subnets | Same format, passed as `-x`. Same limits. |
| Forward DNS | Passes `--dns`. |
| Method | `auto` (sshuttle's choice), `nft`, `nat` or `tproxy`. |
| Disable IPv6 | Passes `--disable-ipv6`. |
| SSH identity file | Optional path passed as `ssh -i`. No whitespace, quotes or backslashes, must not start with `-`. |

Profiles are stored in `~/.config/sshuttle-panel/profiles.json` (mode 0600).
sshuttle is started as `sshuttle -r REMOTE ... -e "ssh -o BatchMode=yes -o ConnectTimeout=15 [-i FILE]" SUBNETS`.

## Passwordless sudo

sshuttle needs root to change the firewall, and it asks for the sudo password
itself. A background process started from a bar widget has no terminal, so
the password prompt cannot be answered and the connection would hang.
Passwordless sudo for sshuttle is therefore required.

The "Set up" button does this:

1. Runs `sshuttle --sudoers-no-modify --sudoers-user YOU` to generate the rule.
2. Checks the text strictly: it must be exactly a `Cmnd_Alias` for
   `/usr/bin/env PYTHONPATH=... /path/to/sshuttle *` plus a single
   `YOU ALL=NOPASSWD: ALIAS` line, with the resolved sshuttle path. Anything
   else is rejected.
3. Validates it with `visudo -c`.
4. Installs it with `pkexec install -m 0440 /dev/stdin /etc/sudoers.d/sshuttle-panel`,
   which asks for your authorization through polkit.

Security trade-off. This is sshuttle's own rule, and sshuttle warns about it:
a user who can run sshuttle as root can run other commands as root, for
example through `--ssh-cmd` or `-e`. Install it only on a machine where you
accept that your user account is effectively root-equivalent.

The install is not atomic. If it is interrupted, `/etc/sudoers.d/sshuttle-panel`
could be left truncated, and sudo may then refuse to read it. Fix it from a root
shell or with `sudo` (if sudo still works) by removing the file:

```bash
sudo rm /etc/sudoers.d/sshuttle-panel
```

Removal is the same command. After removing it the panel shows "Set up" again.

## SSH authentication

ssh runs with `BatchMode=yes`, so it never prompts. Use key-based
authentication (an ssh-agent with the key loaded, or an unencrypted key, or
an identity file set on the profile), and make sure the remote's host key is
already in `~/.ssh/known_hosts` (connect once with plain `ssh` first).

## Troubleshooting

- Press "Show log" in the panel, or run `python3 ctl.py log`.
- Files, in `$XDG_RUNTIME_DIR/sshuttle-panel/` (normally `/run/user/UID/sshuttle-panel/`):
  `sshuttle.log` (output of the current/last run, truncated on each start) and
  `state.json` (pid, profile, start time, and a recorded error if any).
- A connection that is not up within 30 seconds is stopped and reported as
  `connection timed out after 30s`. Override with the environment variable
  `SSHUTTLE_PANEL_CONNECT_TIMEOUT` (seconds) for the process running `ctl.py`.
- Stale state: if the panel shows a state that does not match reality, run
  `python3 ctl.py stop`, or delete `state.json`. A recorded error persists
  until you disconnect or connect again.
- "Set up" shown although the rule is installed: the panel probes
  `sudo -n -l` against the current sshuttle path and caches the result for 30
  seconds. If sshuttle moved (upgrade, different prefix), set up again.
- Other environment overrides: `SSHUTTLE_BIN`, `SSHUTTLE_PANEL_CONFIG_DIR`,
  `SSHUTTLE_PANEL_RUNTIME_DIR`.
- Plugin load problems: check the shell logs, for example
  `journalctl --user -b -g sshuttle`.

`ctl.py` is the backend and can be used directly: `list`, `save`, `delete`,
`set-default`, `start [NAME]`, `stop`, `status`, `log`, `setup-sudo`. Each
prints one JSON object.

## Tests

```bash
python3 -m unittest discover -s tests -v
node --test tests/
```

## License

MIT
