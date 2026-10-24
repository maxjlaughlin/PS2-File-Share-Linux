#!/usr/bin/env bash
# Remove PS2 File Share. Your games and your smb.conf are left in working order.

set -euo pipefail

APP_DIR=/opt/ps2share
CONFIG_DIR=/etc/ps2share
STATE_DIR=/var/lib/ps2share
BIN=/usr/local/bin/ps2share
UNIT=/etc/systemd/system/ps2share.service

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }

# Ask a yes/no question. Falls back to the default when there is no terminal
# (piped into bash, run from a provisioning tool), instead of erroring out.
ask() {
  local prompt="$1" default="$2" reply=""
  # /dev/tty can exist and still fail to open (no controlling terminal), and
  # bash reports that before any 2>/dev/null on the read would apply — so
  # probe it inside a subshell whose stderr is already discarded.
  if ( exec < /dev/tty ) 2>/dev/null; then
    read -r -p "$prompt" reply < /dev/tty || reply=""
  fi
  [ -n "$reply" ] || reply="$default"
  case "$reply" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

[ "$(id -u)" -eq 0 ] || { echo "Run this with sudo." >&2; exit 1; }

bold "PS2 File Share — uninstall"

# Take the share out of Samba first, while the app is still installed.
if [ -x "$BIN" ]; then
  if ask "  Remove the PS2 share from Samba as well? [Y/n] " Y; then
    "$BIN" remove || info "could not reach Samba — skipping"
  else
    info "leaving the Samba share in place"
  fi
fi

if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
  systemctl disable --now ps2share.service >/dev/null 2>&1 || true
  rm -f "$UNIT"
  systemctl daemon-reload >/dev/null 2>&1 || true
else
  rm -f "$UNIT"
fi
rm -f "$BIN"
rm -rf "$APP_DIR"
info "removed the application, its launcher and its service"

if ask "  Also delete settings and usage history? [y/N] " N; then
  rm -rf "$CONFIG_DIR" "$STATE_DIR"
  info "settings and history deleted"
else
  info "kept $CONFIG_DIR and $STATE_DIR"
fi

echo
bold "Done. Your games were not touched."
