"""Small helpers shared by the rest of the package."""

from __future__ import annotations

import logging
import os
import pwd
import grp
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

log = logging.getLogger("ps2share")


@dataclass
class Result:
    """Outcome of an external command."""

    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def text(self) -> str:
        """stdout if there is any, otherwise stderr — whichever carries the message."""
        return (self.out or self.err).strip()


def run(cmd: Sequence[str], timeout: float = 20.0, stdin: Optional[str] = None) -> Result:
    """Run a command, never raising: a missing binary just comes back as a failure."""
    try:
        proc = subprocess.run(
            list(cmd),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return Result(127, "", f"{cmd[0]}: not found")
    except subprocess.TimeoutExpired:
        return Result(124, "", f"{cmd[0]}: timed out after {timeout:g}s")
    except OSError as exc:  # pragma: no cover - defensive
        return Result(1, "", f"{cmd[0]}: {exc}")
    return Result(proc.returncode, proc.stdout or "", proc.stderr or "")


def which(name: str) -> Optional[str]:
    return shutil.which(name)


def first_available(names: Iterable[str]) -> Optional[str]:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def is_root() -> bool:
    return os.geteuid() == 0


def human_bytes(value: float, *, rate: bool = False) -> str:
    """Format a byte count (or a per-second rate) for display."""
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(value)
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    if idx == 0:
        text = f"{size:.0f} {units[idx]}"
    elif size >= 100:
        text = f"{size:.0f} {units[idx]}"
    elif size >= 10:
        text = f"{size:.1f} {units[idx]}"
    else:
        text = f"{size:.2f} {units[idx]}"
    return text + "/s" if rate else text


def human_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours:02d}h"


def atomic_write(path: str, text: str, mode: int = 0o644) -> None:
    """Write a file via a temp file + rename so readers never see a half-written config."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def backup_file(path: str, suffix: str = "") -> Optional[str]:
    """Copy `path` next to itself with a timestamp. Returns the backup path."""
    if not os.path.isfile(path):
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = f"{path}.ps2share-{stamp}{suffix}.bak"
    shutil.copy2(path, dest)
    return dest


def user_exists(name: str) -> bool:
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


def group_of_user(name: str) -> Optional[str]:
    try:
        entry = pwd.getpwnam(name)
    except KeyError:
        return None
    try:
        return grp.getgrgid(entry.pw_gid).gr_name
    except KeyError:
        return None


def owner_of_path(path: str) -> Optional[str]:
    try:
        uid = os.stat(path).st_uid
        return pwd.getpwuid(uid).pw_name
    except (OSError, KeyError):
        return None


def filesystem_of(path: str) -> tuple[str, str]:
    """Return (mount_point, fs_type) for the filesystem holding `path`."""
    try:
        target = os.path.realpath(path)
    except OSError:  # pragma: no cover - defensive
        return ("", "")
    best = ("", "")
    best_len = -1
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mount = parts[1].replace("\\040", " ")
                fstype = parts[2]
                if target == mount or target.startswith(mount.rstrip("/") + "/"):
                    if len(mount) > best_len:
                        best, best_len = (mount, fstype), len(mount)
    except OSError:
        return ("", "")
    return best


def disk_usage(path: str) -> dict:
    try:
        stat = os.statvfs(path)
    except OSError:
        return {"total": 0, "used": 0, "free": 0, "percent": 0.0}
    total = stat.f_frsize * stat.f_blocks
    free = stat.f_frsize * stat.f_bavail
    used = total - stat.f_frsize * stat.f_bfree
    percent = (used / total * 100.0) if total else 0.0
    return {"total": total, "used": used, "free": free, "percent": round(percent, 1)}


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
