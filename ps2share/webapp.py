"""The web UI: a small threaded HTTP server with a JSON API.

Deliberately built on `http.server` rather than a framework — this is a
single-user LAN tool and the whole point is that it runs on a freshly
installed server with nothing but Python on it.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import pwd
import grp
import secrets
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Deque, Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__, fsbrowse, netinfo, opl, samba
from .config import Config, default_state_dir
from .monitor import Monitor, UsageStore
from .util import human_bytes, is_root, run, user_exists, which

log = logging.getLogger("ps2share")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
SESSION_TTL = 12 * 3600
STATE_CACHE_SECONDS = 4.0


class Events:
    """A short in-memory log of what the app did, shown in the UI."""

    def __init__(self, limit: int = 200):
        self._items: Deque[dict] = deque(maxlen=limit)
        self._lock = threading.Lock()

    def add(self, level: str, message: str, detail: str = "") -> None:
        with self._lock:
            self._items.appendleft(
                {"ts": time.time(), "level": level, "message": message, "detail": detail}
            )
        log.info("%s: %s %s", level, message, detail)

    def list(self) -> list:
        with self._lock:
            return list(self._items)


class App:
    """Shared state for the handlers."""

    def __init__(self, config: Config, state_dir: str = ""):
        self.config = config
        self.state_dir = state_dir or default_state_dir()
        os.makedirs(self.state_dir, exist_ok=True)
        self.store = UsageStore(
            os.path.join(self.state_dir, "usage.db"), retention_days=config.retention_days
        )
        self.monitor = Monitor(
            self.store,
            interval=config.sample_interval,
            history_seconds=config.history_seconds,
        )
        self.monitor.share_path = config.share_path
        self.scanner = opl.GameScanner(os.path.join(self.state_dir, "game-cache.json"))
        self.events = Events()
        self.sessions: Dict[str, float] = {}
        self._state_cache: Tuple[float, dict] = (0.0, {})
        self._lock = threading.Lock()
        self._games_cache: Tuple[float, list] = (0.0, [])

    # -- sessions ----------------------------------------------------------

    def new_session(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self.sessions[token] = time.time() + SESSION_TTL
            self._expire_sessions()
        return token

    def _expire_sessions(self) -> None:
        now = time.time()
        for token, expiry in list(self.sessions.items()):
            if expiry < now:
                self.sessions.pop(token, None)

    def valid_session(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            self._expire_sessions()
            return token in self.sessions

    def drop_session(self, token: str) -> None:
        with self._lock:
            self.sessions.pop(token, None)

    # -- assembled state ----------------------------------------------------

    def state(self, force: bool = False) -> dict:
        cached_at, cached = self._state_cache
        if not force and time.time() - cached_at < STATE_CACHE_SECONDS and cached:
            return cached

        cfg = self.config
        status = samba.samba_status()
        health = opl.share_health(
            cfg.share_path, serve_as=samba.resolve_force_user(cfg), writable=cfg.writable
        )
        interfaces = netinfo.interfaces()
        listening = netinfo.read_listening_ports()
        primary = netinfo.primary_ip()

        conf_path = samba.smb_conf_path()
        include_present = False
        try:
            with open(conf_path, "r", encoding="utf-8", errors="replace") as handle:
                include_present = samba.has_include(handle.read())
        except OSError:
            pass

        warnings = []
        if not is_root():
            warnings.append(
                "Running without root: the share cannot be applied and per-connection "
                "file details are hidden. Start the app with sudo or via systemd."
            )
        if not status["installed"]:
            warnings.append(
                "Samba is not installed. Install it first — Debian/Ubuntu: `apt install samba`, "
                "Fedora/RHEL: `dnf install samba`, Arch: `pacman -S samba`."
            )
        if not cfg.ui_password_required:
            warnings.append(
                "This interface has no password. Anyone on your network can change the share. "
                "Set a password under Settings."
            )
        if cfg.applied and 445 not in listening:
            warnings.append("Nothing is listening on port 445 — smbd may not be running.")
        if cfg.access_mode == "user" and not cfg.smb_password_set:
            warnings.append(f"No SMB password has been set for '{cfg.username}' yet.")
        if samba.resolve_force_user(cfg) == "root":
            warnings.append(
                "The share acts as root. Anything that connects can write as root inside it — "
                "pick a different account under Share settings unless you meant this."
            )

        state = {
            "version": __version__,
            "root": is_root(),
            "config": cfg.public_dict(),
            "config_path": cfg.config_path,
            "samba": status,
            "smb_conf": conf_path,
            "include_path": samba.include_path(conf_path),
            "include_present": include_present,
            "health": health,
            "network": {
                "interfaces": interfaces,
                "primary_ip": primary,
                "listening": listening,
                "port_445": 445 in listening,
                "port_139": 139 in listening,
            },
            "firewall": netinfo.firewall_state(),
            "opl": netinfo.opl_settings(cfg.share_name, cfg.access_mode, cfg.username),
            "warnings": warnings,
            "monitor_source": self.monitor.source,
            "has_ss": which("ss") is not None,
            "events": self.events.list()[:40],
        }
        self._state_cache = (time.time(), state)
        return state

    def games(self, force: bool = False) -> list:
        cached_at, cached = self._games_cache
        if not force and time.time() - cached_at < 30 and cached:
            return cached
        games = [game.to_dict() for game in self.scanner.scan(self.config.share_path)]
        self._games_cache = (time.time(), games)
        return games

    def invalidate(self) -> None:
        self._state_cache = (0.0, {})
        self._games_cache = (0.0, [])


# ---------------------------------------------------------------------------
# request handling
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = f"ps2share/{__version__}"
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:  # noqa: D102 - quieten the default
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, content_type: str, extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200, extra: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", extra)

    def _error(self, status: int, message: str, detail: str = "") -> None:
        self._json({"ok": False, "error": message, "detail": detail}, status=status)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > 1_000_000:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _cookie_token(self) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "ps2share_session":
                return unquote(value)
        return ""

    def _authorised(self) -> bool:
        if not self.app.config.ui_password_required:
            return True
        return self.app.valid_session(self._cookie_token())

    # -- routing -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/") :])
        if path == "/api/session":
            return self._json(
                {
                    "ok": True,
                    "auth_required": self.app.config.ui_password_required,
                    "authenticated": self._authorised(),
                    "version": __version__,
                }
            )
        if not path.startswith("/api/"):
            return self._error(404, "Not found")
        if not self._authorised():
            return self._error(401, "Not signed in")

        routes: Dict[str, Callable[[Dict[str, list]], None]] = {
            "/api/state": self._get_state,
            "/api/live": self._get_live,
            "/api/fs": self._get_fs,
            "/api/games": self._get_games,
            "/api/usage": self._get_usage,
            "/api/preview": self._get_preview,
            "/api/logs": self._get_logs,
            "/api/system-users": self._get_system_users,
        }
        handler = routes.get(path)
        if not handler:
            return self._error(404, "Not found")
        try:
            handler(query)
        except Exception as exc:  # pragma: no cover - last-resort guard
            log.exception("GET %s failed", path)
            self._error(500, "Something went wrong", str(exc))

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if not path.startswith("/api/"):
            return self._error(404, "Not found")
        # A custom header cannot be set by a cross-origin form post, which is
        # enough CSRF protection for a cookie-authenticated LAN tool.
        if self.headers.get("X-PS2Share") != "1":
            return self._error(400, "Missing X-PS2Share header")

        body = self._body()

        if path == "/api/login":
            return self._post_login(body)

        if not self._authorised():
            return self._error(401, "Not signed in")

        routes: Dict[str, Callable[[dict], None]] = {
            "/api/logout": self._post_logout,
            "/api/share": self._post_share,
            "/api/unshare": self._post_unshare,
            "/api/prepare": self._post_prepare,
            "/api/permissions": self._post_permissions,
            "/api/service": self._post_service,
            "/api/firewall": self._post_firewall,
            "/api/ui-password": self._post_ui_password,
            "/api/smb-password": self._post_smb_password,
            "/api/mkdir": self._post_mkdir,
            "/api/settings": self._post_settings,
        }
        handler = routes.get(path)
        if not handler:
            return self._error(404, "Not found")
        try:
            handler(body)
        except Exception as exc:  # pragma: no cover - last-resort guard
            log.exception("POST %s failed", path)
            self._error(500, "Something went wrong", str(exc))

    # -- static ------------------------------------------------------------

    def _static(self, relative: str) -> None:
        safe = os.path.normpath(relative).lstrip("./")
        full = os.path.join(STATIC_DIR, safe)
        if not os.path.abspath(full).startswith(STATIC_DIR) or not os.path.isfile(full):
            return self._error(404, "Not found")
        content_type = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript",):
            content_type += "; charset=utf-8"
        with open(full, "rb") as handle:
            self._send(200, handle.read(), content_type)

    # -- GET handlers ------------------------------------------------------

    def _get_state(self, query: Dict[str, list]) -> None:
        force = query.get("force", ["0"])[0] == "1"
        self._json({"ok": True, "state": self.app.state(force=force)})

    def _get_live(self, query: Dict[str, list]) -> None:
        snapshot = self.app.monitor.snapshot()
        snapshot["ok"] = True
        snapshot["share_path"] = self.app.config.share_path
        self._json(snapshot)

    def _get_fs(self, query: Dict[str, list]) -> None:
        path = query.get("path", ["/"])[0]
        payload = fsbrowse.listdir(path)
        payload["ok"] = True
        payload["suggestions"] = fsbrowse.suggestions()
        self._json(payload)

    def _get_games(self, query: Dict[str, list]) -> None:
        force = query.get("force", ["0"])[0] == "1"
        games = self.app.games(force=force)
        total = sum(int(game["size"]) for game in games)
        self._json(
            {
                "ok": True,
                "games": games,
                "count": len(games),
                "total_size": total,
                "total_size_h": human_bytes(total),
                "memory_cards": opl.list_memory_cards(self.app.config.share_path),
                "folders": opl.layout_state(self.app.config.share_path)
                if self.app.config.share_path
                else [],
            }
        )

    def _get_usage(self, query: Dict[str, list]) -> None:
        try:
            days = max(1, min(90, int(query.get("days", ["14"])[0])))
        except ValueError:
            days = 14
        now = time.time()
        midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        self._json(
            {
                "ok": True,
                "today": _totals_dict(self.app.store.totals(midnight)),
                "week": _totals_dict(self.app.store.totals(now - 7 * 86400)),
                "month": _totals_dict(self.app.store.totals(now - 30 * 86400)),
                "all_time": _totals_dict(self.app.store.totals(0)),
                "daily": self.app.store.daily(days),
                "clients": [
                    dict(row, total_h=human_bytes(row["total"]))
                    for row in self.app.store.per_client(now - days * 86400)
                ],
                "days": days,
            }
        )

    def _get_preview(self, query: Dict[str, list]) -> None:
        text = samba.render_config(self.app.config)
        conf = samba.smb_conf_path()
        self._json(
            {
                "ok": True,
                "config": text,
                "include_path": samba.include_path(conf),
                "smb_conf": conf,
                "testparm": samba.testparm().text if samba.samba_installed() else "",
            }
        )

    def _get_logs(self, query: Dict[str, list]) -> None:
        try:
            lines = max(10, min(500, int(query.get("lines", ["120"])[0])))
        except ValueError:
            lines = 120
        self._json(
            {
                "ok": True,
                "samba": _tail_samba_log(lines),
                "events": self.app.events.list(),
            }
        )

    def _get_system_users(self, query: Dict[str, list]) -> None:
        users = []
        for entry in pwd.getpwall():
            if entry.pw_uid == 0 or 1000 <= entry.pw_uid < 65534:
                users.append({"name": entry.pw_name, "uid": entry.pw_uid, "home": entry.pw_dir})
        users.sort(key=lambda row: row["uid"])
        self._json({"ok": True, "users": users})

    # -- POST handlers -----------------------------------------------------

    def _post_login(self, body: dict) -> None:
        cfg = self.app.config
        if not cfg.ui_password_required:
            return self._json({"ok": True, "authenticated": True})
        password = str(body.get("password") or "")
        # A small constant delay blunts online guessing without being annoying.
        time.sleep(0.25)
        if not cfg.check_ui_password(password):
            self.app.events.add("warn", "Failed sign-in attempt", self.address_string())
            return self._error(401, "Wrong password")
        token = self.app.new_session()
        cookie = f"ps2share_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}"
        self._json({"ok": True, "authenticated": True}, extra={"Set-Cookie": cookie})

    def _post_logout(self, body: dict) -> None:
        self.app.drop_session(self._cookie_token())
        self._json(
            {"ok": True},
            extra={"Set-Cookie": "ps2share_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"},
        )

    def _post_settings(self, body: dict) -> None:
        """Update stored settings without touching Samba."""
        cfg = self.app.config
        for key in (
            "share_path", "share_name", "access_mode", "username", "workgroup",
            "force_user", "writable", "smb1_only", "lanman_auth", "enable_netbios",
            "tuned_performance", "wide_links",
        ):
            if key in body:
                setattr(cfg, key, body[key])
        cfg.normalise()
        cfg.save()
        self.app.monitor.share_path = cfg.share_path
        self.app.invalidate()
        self._json({"ok": True, "config": cfg.public_dict()})

    def _post_share(self, body: dict) -> None:
        """Save the share settings and push them into Samba."""
        cfg = self.app.config
        before = cfg.share_path

        share_path = str(body.get("share_path") or cfg.share_path).strip()
        if not share_path:
            return self._error(400, "Choose a folder to share.")
        if not os.path.isdir(share_path):
            return self._error(400, f"{share_path} is not a folder.")

        cfg.share_path = share_path
        for key in (
            "share_name", "access_mode", "username", "workgroup", "force_user",
            "writable", "smb1_only", "lanman_auth", "enable_netbios", "tuned_performance",
            "wide_links",
        ):
            if key in body:
                setattr(cfg, key, body[key])
        cfg.normalise()

        if cfg.access_mode == "user":
            password = str(body.get("smb_password") or "")
            if not user_exists(cfg.username):
                if not body.get("create_user"):
                    return self._error(
                        400,
                        f"There is no Linux account called '{cfg.username}'.",
                        "Tick 'create the account for me', or pick an account that already exists.",
                    )
                created = samba.create_system_user(cfg.username, home=cfg.share_path)
                if not created.ok:
                    return self._error(400, "Could not create the account.", created.text)
                self.app.events.add("info", f"Created system account '{cfg.username}'")
            if password:
                result = samba.set_smb_password(cfg.username, password)
                if not result.ok:
                    return self._error(400, "Could not set the SMB password.", result.text)
                cfg.smb_password_set = True
                self.app.events.add("info", f"SMB password set for '{cfg.username}'")
            elif not (cfg.smb_password_set or samba.smb_user_exists(cfg.username)):
                return self._error(400, f"Set a password for '{cfg.username}'.")
            else:
                cfg.smb_password_set = True

        if body.get("prepare_folders", True):
            uid = gid = None
            force_user = samba.resolve_force_user(cfg)
            if is_root() and user_exists(force_user):
                entry = pwd.getpwnam(force_user)
                uid, gid = entry.pw_uid, entry.pw_gid
            try:
                created = opl.ensure_layout(cfg.share_path, uid, gid)
            except OSError as exc:
                return self._error(400, "Could not create the OPL folders.", str(exc))
            if created:
                self.app.events.add("info", "Created OPL folders: " + ", ".join(created))

        outcome = samba.apply(cfg)
        if outcome.written:
            # Valid config on disk: remember it even if the service would not start.
            cfg.applied = True
            cfg.save()
            self.app.monitor.share_path = cfg.share_path
        elif before != cfg.share_path:
            cfg.share_path = before
        self.app.events.add(
            "info" if outcome.ok else "error", outcome.message, outcome.details
        )
        self.app.invalidate()
        self._json(
            {
                "ok": outcome.ok,
                "message": outcome.message,
                "detail": outcome.details,
                "config": cfg.public_dict(),
                "error": None if outcome.ok else outcome.message,
            },
            status=200 if outcome.ok else 400,
        )

    def _post_unshare(self, body: dict) -> None:
        outcome = samba.unapply()
        if outcome.ok:
            self.app.config.applied = False
            self.app.config.save()
        self.app.events.add("info" if outcome.ok else "error", outcome.message, outcome.details)
        self.app.invalidate()
        self._json({"ok": outcome.ok, "message": outcome.message, "detail": outcome.details})

    def _post_prepare(self, body: dict) -> None:
        path = str(body.get("path") or self.app.config.share_path)
        if not path or not os.path.isdir(path):
            return self._error(400, "Pick a folder first.")
        uid = gid = None
        force_user = samba.resolve_force_user(self.app.config)
        if is_root() and user_exists(force_user):
            entry = pwd.getpwnam(force_user)
            uid, gid = entry.pw_uid, entry.pw_gid
        try:
            created = opl.ensure_layout(path, uid, gid)
        except OSError as exc:
            return self._error(400, "Could not create the folders.", str(exc))
        self.app.invalidate()
        message = ("Created: " + ", ".join(created)) if created else "All OPL folders were already there."
        self.app.events.add("info", message)
        self._json({"ok": True, "created": created, "message": message})

    def _post_permissions(self, body: dict) -> None:
        if not is_root():
            return self._error(400, "Fixing permissions needs root.")
        cfg = self.app.config
        if not cfg.share_path or not os.path.isdir(cfg.share_path):
            return self._error(400, "Pick a folder first.")
        force_user = samba.resolve_force_user(cfg)
        if not user_exists(force_user):
            return self._error(400, f"No such account: {force_user}")
        entry = pwd.getpwnam(force_user)
        changed, errors = opl.fix_permissions(cfg.share_path, entry.pw_uid, entry.pw_gid)
        message = f"Updated {changed} file{'s' if changed != 1 else ''} to {force_user}:{grp.getgrgid(entry.pw_gid).gr_name}."
        self.app.events.add("info", message, "; ".join(errors))
        self._json({"ok": True, "changed": changed, "errors": errors, "message": message})

    def _post_service(self, body: dict) -> None:
        action = str(body.get("action") or "")
        result = samba.control_service(action)
        self.app.events.add(
            "info" if result.ok else "error", f"Samba {action}", result.text[:400]
        )
        self.app.invalidate()
        self._json({"ok": result.ok, "message": result.text or f"smbd {action} done.", "detail": result.text})

    def _post_firewall(self, body: dict) -> None:
        if not is_root():
            return self._error(400, "Changing the firewall needs root.")
        network = str(body.get("network") or "")
        result = netinfo.open_firewall(network)
        self.app.events.add("info" if result["ok"] else "error", str(result["message"]))
        self.app.invalidate()
        self._json({"ok": result["ok"], "message": result["message"], "ran": result["ran"]})

    def _post_ui_password(self, body: dict) -> None:
        cfg = self.app.config
        new = str(body.get("password") or "")
        if cfg.ui_password_required:
            if not cfg.check_ui_password(str(body.get("current") or "")):
                return self._error(401, "Current password is wrong.")
        if new and len(new) < 6:
            return self._error(400, "Use at least 6 characters.")
        cfg.set_ui_password(new)
        cfg.save()
        self.app.invalidate()
        if new:
            token = self.app.new_session()
            cookie = f"ps2share_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}"
            self.app.events.add("info", "Web UI password set")
            return self._json({"ok": True, "message": "Password set."}, extra={"Set-Cookie": cookie})
        self.app.events.add("warn", "Web UI password removed")
        self._json({"ok": True, "message": "Password removed — the UI is now open to your network."})

    def _post_smb_password(self, body: dict) -> None:
        cfg = self.app.config
        password = str(body.get("password") or "")
        username = str(body.get("username") or cfg.username)
        if not password:
            return self._error(400, "Password must not be empty.")
        result = samba.set_smb_password(username, password)
        if not result.ok:
            return self._error(400, "Could not set the SMB password.", result.text)
        cfg.username = username
        cfg.smb_password_set = True
        cfg.save()
        self.app.invalidate()
        self.app.events.add("info", f"SMB password updated for '{username}'")
        self._json({"ok": True, "message": f"SMB password updated for {username}."})

    def _post_mkdir(self, body: dict) -> None:
        path = str(body.get("path") or "").strip()
        if not path:
            return self._error(400, "No path given.")
        result = fsbrowse.create_directory(path)
        self._json({"ok": result["ok"], "message": result["message"], "path": result["path"]})


def _totals_dict(totals: Tuple[int, int]) -> dict:
    tx, rx = totals
    return {
        "tx": tx,
        "rx": rx,
        "total": tx + rx,
        "tx_h": human_bytes(tx),
        "rx_h": human_bytes(rx),
        "total_h": human_bytes(tx + rx),
    }


SAMBA_LOG_DIRS = ("/var/log/samba", "/var/log")


def _tail_samba_log(lines: int) -> dict:
    """Tail whichever smbd log this distribution writes."""
    candidates = []
    for directory in SAMBA_LOG_DIRS:
        for name in ("log.smbd", "samba.log", "smbd.log"):
            full = os.path.join(directory, name)
            if os.path.isfile(full):
                candidates.append(full)
    if not candidates:
        result = run(["journalctl", "-u", "smbd", "-n", str(lines), "--no-pager"], timeout=15)
        if result.ok:
            return {"source": "journalctl -u smbd", "text": result.out}
        return {"source": "", "text": "No Samba log found."}
    path = max(candidates, key=lambda item: os.path.getmtime(item))
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            window = min(size, lines * 400)
            handle.seek(size - window)
            text = handle.read().decode("utf-8", "replace")
    except OSError as exc:
        return {"source": path, "text": f"Could not read {path}: {exc.strerror}"}
    return {"source": path, "text": "\n".join(text.splitlines()[-lines:])}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], app: App):
        self.app = app
        super().__init__(address, Handler)


def serve(config: Config, *, state_dir: str = "") -> None:
    app = App(config, state_dir=state_dir)
    app.monitor.start()
    app.events.add("info", f"PS2 File Share {__version__} started")

    try:
        server = Server((config.ui_host, config.ui_port), app)
    except OSError as exc:
        raise SystemExit(
            f"Could not listen on {config.ui_host}:{config.ui_port} — {exc.strerror}. "
            "Is another copy already running, or is the port in use?"
        )

    display_host = config.ui_host if config.ui_host not in ("0.0.0.0", "::") else (netinfo.primary_ip() or "localhost")
    print(f"PS2 File Share {__version__}")
    print(f"  Web interface : http://{display_host}:{config.ui_port}/")
    print(f"  Config        : {config.config_path}")
    print(f"  Running as    : {'root' if is_root() else 'an unprivileged user (limited)'}")
    if not config.ui_password_required:
        print("  WARNING       : no UI password is set — anyone on your network can change the share.")
    print("Press Ctrl-C to stop.")

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nShutting down…")
    finally:
        server.shutdown()
        app.monitor.stop()
        app.store.close()
