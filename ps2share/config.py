"""Persistent application settings."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, Optional

from .util import atomic_write, is_root

SYSTEM_CONFIG_DIR = "/etc/ps2share"
SYSTEM_STATE_DIR = "/var/lib/ps2share"


def default_config_dir() -> str:
    if is_root():
        return SYSTEM_CONFIG_DIR
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "ps2share")


def default_state_dir() -> str:
    if is_root():
        return SYSTEM_STATE_DIR
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "ps2share")


@dataclass
class Config:
    """Everything the app remembers between runs.

    Passwords are never stored here: the SMB password lives in Samba's own
    database, and the web UI password is kept only as a PBKDF2 hash.
    """

    # --- the share ------------------------------------------------------
    share_path: str = ""
    share_name: str = "PS2SMB"
    access_mode: str = "guest"  # "guest" | "user"
    username: str = "ps2"
    smb_password_set: bool = False
    writable: bool = True
    force_user: str = ""  # blank = pick the owner of share_path
    workgroup: str = "WORKGROUP"

    # --- PS2 / OPL compatibility knobs ----------------------------------
    smb1_only: bool = False  # cap the server at NT1 instead of allowing SMB2/3
    lanman_auth: bool = False  # only needed by very old OPL builds
    enable_netbios: bool = True  # also listen on port 139
    tuned_performance: bool = True  # sendfile / aio / TCP_NODELAY block
    wide_links: bool = True  # let symlinks in the share point at other disks

    # --- the web UI ------------------------------------------------------
    ui_host: str = "0.0.0.0"
    ui_port: int = 8723
    ui_password_hash: str = ""
    ui_password_salt: str = ""

    # --- monitoring -------------------------------------------------------
    sample_interval: float = 1.0
    history_seconds: int = 300
    retention_days: int = 30

    # --- bookkeeping ------------------------------------------------------
    applied: bool = False  # has a share config been written to Samba yet
    config_path: str = field(default="", compare=False, repr=False)

    # -- persistence -------------------------------------------------------

    @classmethod
    def path_for(cls, explicit: Optional[str] = None) -> str:
        if explicit:
            return explicit
        env = os.environ.get("PS2SHARE_CONFIG")
        if env:
            return env
        return os.path.join(default_config_dir(), "config.json")

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        target = cls.path_for(path)
        cfg = cls()
        cfg.config_path = target
        try:
            with open(target, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            return cfg
        if not isinstance(raw, dict):
            return cfg
        known = {f.name for f in fields(cls)} - {"config_path"}
        for key, value in raw.items():
            if key in known:
                setattr(cfg, key, value)
        cfg.normalise()
        return cfg

    def save(self) -> None:
        target = self.config_path or self.path_for()
        self.config_path = target
        data = asdict(self)
        data.pop("config_path", None)
        atomic_write(target, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=0o600)

    def normalise(self) -> None:
        if self.access_mode not in ("guest", "user"):
            self.access_mode = "guest"
        self.share_name = (self.share_name or "PS2SMB").strip() or "PS2SMB"
        self.username = (self.username or "ps2").strip() or "ps2"
        self.workgroup = (self.workgroup or "WORKGROUP").strip() or "WORKGROUP"
        try:
            self.ui_port = int(self.ui_port)
        except (TypeError, ValueError):
            self.ui_port = 8723
        self.sample_interval = max(0.5, min(10.0, float(self.sample_interval or 1.0)))
        self.history_seconds = max(60, min(3600, int(self.history_seconds or 300)))
        self.retention_days = max(1, min(365, int(self.retention_days or 30)))

    # -- UI password -------------------------------------------------------

    @property
    def ui_password_required(self) -> bool:
        return bool(self.ui_password_hash and self.ui_password_salt)

    def set_ui_password(self, password: str) -> None:
        if not password:
            self.ui_password_hash = ""
            self.ui_password_salt = ""
            return
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
        self.ui_password_salt = base64.b64encode(salt).decode("ascii")
        self.ui_password_hash = base64.b64encode(digest).decode("ascii")

    def check_ui_password(self, password: str) -> bool:
        if not self.ui_password_required:
            return True
        try:
            salt = base64.b64decode(self.ui_password_salt)
            expected = base64.b64decode(self.ui_password_hash)
        except (ValueError, TypeError):
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
        return hmac.compare_digest(digest, expected)

    # -- views -------------------------------------------------------------

    def public_dict(self) -> Dict[str, Any]:
        """The parts of the config that are safe to hand to the browser."""
        data = asdict(self)
        data.pop("config_path", None)
        data.pop("ui_password_hash", None)
        data.pop("ui_password_salt", None)
        data["ui_password_required"] = self.ui_password_required
        return data
