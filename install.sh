#!/usr/bin/env bash
# Install PS2 File Share as a systemd service.
#
#   sudo ./install.sh
#
# Everything lands in three places and nothing else on the system is touched:
#   /opt/ps2share        the application
#   /etc/ps2share        its settings
#   /var/lib/ps2share    usage history
#
# Your smb.conf is never rewritten — the app appends one marked include line
# the first time you apply a share, and `uninstall.sh` takes it back out.

set -euo pipefail

APP_DIR=/opt/ps2share
CONFIG_DIR=/etc/ps2share
STATE_DIR=/var/lib/ps2share
BIN=/usr/local/bin/ps2share
UNIT=/etc/systemd/system/ps2share.service
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
info()  { printf '  %s\n' "$*"; }
warn()  { printf '\033[33m  ! %s\033[0m\n' "$*"; }
fail()  { printf '\033[31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }

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

[ "$(id -u)" -eq 0 ] || fail "Run this with sudo."

bold "PS2 File Share — install"

# ── python ────────────────────────────────────────────────────────────
command -v python3 >/dev/null 2>&1 || fail "python3 is not installed."
python3 - <<'PY' || fail "Python 3.8 or newer is required."
import sys
sys.exit(0 if sys.version_info >= (3, 8) else 1)
PY
info "Python $(python3 -c 'import platform; print(platform.python_version())') — ok"

# ── samba ─────────────────────────────────────────────────────────────
if command -v smbd >/dev/null 2>&1; then
  info "Samba $(smbd --version 2>/dev/null | sed 's/Version //') — ok"
else
  warn "Samba is not installed; the app cannot share anything without it."
  if   command -v apt-get >/dev/null 2>&1; then INSTALL="apt-get install -y samba"
  elif command -v dnf     >/dev/null 2>&1; then INSTALL="dnf install -y samba"
  elif command -v yum     >/dev/null 2>&1; then INSTALL="yum install -y samba"
  elif command -v pacman  >/dev/null 2>&1; then INSTALL="pacman -S --noconfirm samba"
  elif command -v zypper  >/dev/null 2>&1; then INSTALL="zypper install -y samba"
  elif command -v apk     >/dev/null 2>&1; then INSTALL="apk add samba"
  else INSTALL=""
  fi
  if [ -n "$INSTALL" ]; then
    if ask "    Install it now with '$INSTALL'? [Y/n] " Y; then
      $INSTALL
    else
      warn "Skipped — install Samba yourself before using the app."
    fi
  else
    warn "Unknown package manager — install the 'samba' package yourself."
  fi
fi

command -v ss >/dev/null 2>&1 || warn "iproute2 ('ss') is missing — bandwidth figures will be estimated rather than measured."

# ── files ─────────────────────────────────────────────────────────────
[ -d "$SOURCE_DIR/ps2share" ] || fail "Run this from the directory you cloned, it must contain ps2share/."

bold "Installing"
install -d -m 0755 "$APP_DIR" "$CONFIG_DIR" "$STATE_DIR"
rm -rf "${APP_DIR:?}/ps2share"
cp -r "$SOURCE_DIR/ps2share" "$APP_DIR/ps2share"
find "$APP_DIR/ps2share" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
chmod -R go-w "$APP_DIR"
chmod 0700 "$CONFIG_DIR"
info "application  → $APP_DIR"

cat > "$BIN" <<EOF
#!/bin/sh
# PS2 File Share launcher
PYTHONPATH=$APP_DIR exec python3 -m ps2share "\$@"
EOF
chmod 0755 "$BIN"
info "command      → $BIN"

if [ -f "$SOURCE_DIR/systemd/ps2share.service" ]; then
  sed "s#@APP_DIR@#$APP_DIR#g" "$SOURCE_DIR/systemd/ps2share.service" > "$UNIT"
  chmod 0644 "$UNIT"
  info "service      → $UNIT"
fi

# ── service ───────────────────────────────────────────────────────────
# /run/systemd/system only exists when systemd is actually PID 1 — `systemctl`
# is present in plenty of containers where it cannot do anything.
if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1 && [ -f "$UNIT" ]; then
  systemctl daemon-reload || true
  systemctl enable ps2share.service >/dev/null 2>&1 || true
  if systemctl restart ps2share.service >/dev/null 2>&1; then
    sleep 1
  fi
  if systemctl is-active --quiet ps2share.service; then
    info "service started and enabled at boot"
  else
    warn "the service did not start — check: journalctl -u ps2share -n 40"
  fi
else
  rm -f "$UNIT"
  warn "systemd is not running here, so no service was installed."
  warn "Start it yourself with:  ps2share serve"
fi

# ── where to go next ──────────────────────────────────────────────────
PORT=8723
IP="$(python3 - <<'PY'
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("198.51.100.1", 9))
    print(s.getsockname()[0])
except OSError:
    print("localhost")
finally:
    s.close()
PY
)"

echo
bold "Done."
echo "  Open  http://$IP:$PORT/  from any machine on your network."

# A firewall blocking the web interface cannot be opened from the web
# interface, so this has to be dealt with here or from the terminal.
FW=""
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  FW="ufw"
elif command -v firewall-cmd >/dev/null 2>&1 && [ "$(firewall-cmd --state 2>/dev/null)" = "running" ]; then
  FW="firewalld"
fi
if [ -n "$FW" ]; then
  echo
  warn "$FW is active and will probably block port $PORT and SMB."
  if ask "    Allow them through now? [Y/n] " Y; then
    "$BIN" open-firewall || warn "could not change the firewall — run 'sudo ps2share open-firewall' yourself"
  else
    warn "Run this when you are ready:  sudo ps2share open-firewall"
  fi
fi
echo
echo "  Set a password for that page first:   sudo ps2share set-ui-password"
echo "  Or skip the web UI entirely:          sudo ps2share apply /path/to/games"
echo "  Remove everything again:              sudo ./uninstall.sh"
echo
