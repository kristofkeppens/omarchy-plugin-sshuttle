#!/usr/bin/env bash
# Link this checkout into Omarchy's user plugin directory as sshuttle.panel and
# ask the shell to pick it up. Safe to run again: an existing link is replaced,
# but a real directory or file at that path is never touched.
set -euo pipefail

src="$(cd "$(dirname "$0")" && pwd)"
plugins="$HOME/.config/omarchy/plugins"
target="$plugins/sshuttle.panel"

if [ -e "$target" ] && [ ! -L "$target" ]; then
  echo "install.sh: $target exists and is not a symlink; refusing to replace it." >&2
  echo "install.sh: move it away first if it is an old copy of sshuttle.panel." >&2
  exit 1
fi

mkdir -p "$plugins"
# -n: replace an existing link itself instead of creating a link inside the
# directory it points to.
ln -sfn "$src" "$target"
echo "Linked $target -> $(readlink "$target")"

if command -v omarchy-shell >/dev/null 2>&1; then
  omarchy-shell shell rescanPlugins || echo "install.sh: could not ask omarchy-shell to rescan plugins; it will pick the plugin up on its next reload." >&2
else
  echo "install.sh: omarchy-shell not found; the plugin loads the next time the shell starts." >&2
fi
