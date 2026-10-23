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

[ "$(id -u)" -eq 0 ] || { echo "Run this with sudo." >&2; exit 1; }

bold "PS2 File Share — uninstall"

# Take the share out of Samba first, while the app is still installed.
if [ -x "$BIN" ]; then
  read -r -p "  Remove the PS2 share from Samba as well? [Y/n] " reply </dev/tty || reply=Y
  case "${reply:-Y}" in
    [Yy]*) "$BIN" remove || info "could not reach Samba — skipping" ;;
    *)     info "leaving the Samba share in place" ;;
  esac
fi

if command -v systemctl >/dev/null 2>&1; then
  systemctl disable --now ps2share.service >/dev/null 2>&1 || true
fi
rm -f "$UNIT"
command -v systemctl >/dev/null 2>&1 && systemctl daemon-reload || true
rm -f "$BIN"
rm -rf "$APP_DIR"
info "removed the application, its launcher and its service"

read -r -p "  Also delete settings and usage history? [y/N] " purge </dev/tty || purge=N
case "${purge:-N}" in
  [Yy]*) rm -rf "$CONFIG_DIR" "$STATE_DIR"; info "settings and history deleted" ;;
  *)     info "kept $CONFIG_DIR and $STATE_DIR" ;;
esac

echo
bold "Done. Your games were not touched."
