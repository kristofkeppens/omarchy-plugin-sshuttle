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

Installing the plugin does not put it on the bar. Add it once with:

```bash
omarchy plugin enable sshuttle.panel --section right
```

(`omarchy bar put sshuttle.panel --section right` does the same placement.)
`omarchy bar move` only repositions a widget that is already on the bar; on a
widget that has not been added yet it fails with
`could not find widget sshuttle.panel`.

You can also add an entry to the bar layout in `~/.config/omarchy/shell.json`
yourself (it hot-reloads on save):

```json
{"id": "sshuttle.panel", "showLabel": true}
```

Valid sections, in the order they run along the bar: `left`, `centerBegin`,
`center`, `centerEnd`, `right`. To sit beside the clock use `centerBegin` or
`centerEnd`; putting it in `center` moves the clock off center. Once it is on
the bar, `omarchy bar move sshuttle.panel --section centerEnd` relocates it
without a restart.

Settings (inline keys on the entry):

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `iconSize` | integer, 8-24 | 18 | Icon size |
| `refreshIntervalSec` | integer, 2-60 | 5 | Status refresh interval in seconds |
| `showLabel` | boolean | false | Show the active profile name next to the icon |

## Using the panel

The status is one of Connected, Connecting, Disconnected or Error. In the error
state the message (usually the last line of the sshuttle log, or the `fatal:`
line sshuttle printed after connecting, for example when the firewall step
fails) is shown and stays until you press "Clear error" on the error card or
connect again. "Clear error" runs `stop`, which also ends a leftover process.
If sshuttle is not installed the panel says so.

- The switch in the header connects or disconnects. It connects the selected
  profile (the default profile, else the first). While passwordless sudo is
  not set up the switch cannot connect (sshuttle would hang waiting for a
  password); use "Set up" first. It can always disconnect.
- Profiles are listed below; select one, mark one as default, add, edit or
  delete. Names must be unique: the panel refuses to save a profile under a
  name another profile already has. (`ctl.py save` on the command line does
  replace a profile with the same name.)
- "Show log" displays the last 50 lines of sshuttle's output.
- If passwordless sudo is not set up the panel shows a "Set up" button, see
  below.

## Profile fields

| Field | Rules |
| --- | --- |
| Name | 1-40 characters, not blank, no control characters. |
| Remote | `[user@]host[:port]`. Host is a hostname, IPv4 address or `[IPv6]`. Port 1-65535. Max 300 characters. |
| Subnets | One per line, `addr[/mask][:port[-port]]`. Empty means `0/0` (all traffic). Max 200 entries of 100 characters. |
| Excluded subnets or hostnames | Each entry is either a subnet (same format as above) or a hostname with an optional port or port range, e.g. `intranet.example.com` or `example.com:8000-8080`. Passed as `-x`. A hostname takes no `/mask`. sshuttle resolves hostnames itself, on this machine, when it connects, so a name that does not resolve fails the connection with `Unable to resolve address`, and a name that changes address later is not re-resolved until you reconnect. Only excludes take hostnames; the subnets to forward stay addresses and ranges. Same limits. |
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

The "Set up" button first asks for confirmation (Cancel is preselected): the
rule lets any program running as your user run commands as root without a
password, through sshuttle. Only after you confirm it does this:

1. Runs `sshuttle --sudoers-no-modify --sudoers-user YOU` to generate the rule.
2. Checks the text strictly: it must be exactly a `Cmnd_Alias` for
   `/usr/bin/env PYTHONPATH=... /path/to/sshuttle *` plus a single
   `YOU ALL=NOPASSWD: ALIAS` line, with the resolved sshuttle path. Anything
   else is rejected.
3. Validates it with `visudo -c`.
4. Installs it with `pkexec /usr/bin/install -m 0440 /dev/stdin /etc/sudoers.d/sshuttle-panel`,
   which asks for your authorization through polkit.

Whether sudo is ready is checked with
`sudo -n -l -l /usr/bin/env PYTHONPATH=... /path/to/sshuttle --firewall`
(the same check `omarchy-dns` uses): it counts as ready only if sudo permits
that command and the matching rule's `Options:` line shows `!authenticate`,
that is, the rule is NOPASSWD. A plain `sudo -l` is not enough: the blanket
`%wheel ALL=(ALL) ALL` rule permits everything but still asks for a password.

The rule names the `PYTHONPATH` of the Python sshuttle runs on (for example
`/usr/lib/python3.14/site-packages`). After a Python minor-version upgrade
(3.14 to 3.15) that path changes, the old rule no longer matches, and the
panel shows "Set up" again. Press it again; the new rule replaces the old
file.

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
  `sshuttle.log` (output of the current/last run, truncated on each start),
  `state.json` (pid, profile, start time, and a recorded error if any) and
  `sudo.json` (the cached sudo check).
- A connection that is not up within 30 seconds is stopped and reported as
  `connection timed out after 30s`. Override with the environment variable
  `SSHUTTLE_PANEL_CONNECT_TIMEOUT` (seconds) for the process running `ctl.py`.
- Stale state: if the panel shows a state that does not match reality, run
  `python3 ctl.py stop` (or press "Clear error"). Do not delete `state.json`
  by hand: it holds the pid of a running tunnel, and without it `stop` can no
  longer find and end that process. A recorded error persists until you clear
  it or connect again.
- "Set up" shown although the rule is installed: the panel checks with
  `sudo -n -l -l` (see above) against the current sshuttle path and caches the
  answer, ready or not, for 60 seconds in `sudo.json` in the runtime
  directory. The cache is dropped after "Set up" and whenever a connection
  fails. If sshuttle moved (upgrade, different prefix) or Python was upgraded,
  set up again.
- Environment overrides for the process running `ctl.py`:
  `SSHUTTLE_BIN` (sshuttle to run), `SUDO_BIN`, `PKEXEC_BIN`, `VISUDO_BIN`
  (the tools used for the sudo check and setup), `SSHUTTLE_PANEL_CONFIG_DIR`
  (profiles), `SSHUTTLE_PANEL_RUNTIME_DIR` (state, log, sudo cache; defaults
  to `$XDG_RUNTIME_DIR/sshuttle-panel`), `SSHUTTLE_PANEL_CONNECT_TIMEOUT`
  (seconds, default 30) and `SSHUTTLE_PANEL_SUDO_TTL` (seconds the sudo check
  is cached, default 60).
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
