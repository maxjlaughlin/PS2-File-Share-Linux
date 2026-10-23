"""The folder picker's backend: list directories and suggest sensible starting points."""

from __future__ import annotations

import os
from typing import Dict, List

from .util import disk_usage, human_bytes

# Pseudo-filesystems that are never where someone keeps their games.
SKIP_FSTYPES = {
    "proc", "sysfs", "devtmpfs", "devpts", "securityfs", "cgroup", "cgroup2",
    "pstore", "efivarfs", "bpf", "debugfs", "tracefs", "configfs", "fusectl",
    "mqueue", "hugetlbfs", "ramfs", "binfmt_misc", "autofs", "nsfs", "squashfs",
    "overlay", "tmpfs",
}

INTERESTING_PREFIXES = ("/mnt", "/media", "/srv", "/home", "/data", "/storage", "/export", "/games", "/opt")


def listdir(path: str) -> Dict[str, object]:
    """Directories inside `path`, plus the breadcrumb trail to get back."""
    target = os.path.abspath(os.path.expanduser(path or "/"))
    if not os.path.isdir(target):
        target = "/"

    entries: List[Dict[str, object]] = []
    error = ""
    try:
        with os.scandir(target) as scan:
            for entry in scan:
                if entry.name.startswith("."):
                    continue
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                full = os.path.join(target, entry.name)
                entries.append(
                    {
                        "name": entry.name,
                        "path": full,
                        "writable": os.access(full, os.W_OK),
                        "readable": os.access(full, os.R_OK | os.X_OK),
                    }
                )
    except PermissionError:
        error = f"No permission to read {target}."
    except OSError as exc:
        error = f"{target}: {exc.strerror}"

    entries.sort(key=lambda row: str(row["name"]).lower())

    crumbs = [{"name": "/", "path": "/"}]
    walked = ""
    for part in target.strip("/").split("/"):
        if not part:
            continue
        walked += "/" + part
        crumbs.append({"name": part, "path": walked})

    usage = disk_usage(target)
    return {
        "path": target,
        "parent": os.path.dirname(target.rstrip("/")) or "/",
        "entries": entries,
        "crumbs": crumbs,
        "error": error,
        "writable": os.access(target, os.W_OK),
        "disk": usage,
        "disk_h": {
            "total": human_bytes(usage["total"]),
            "free": human_bytes(usage["free"]),
            "used": human_bytes(usage["used"]),
        },
    }


def mount_points() -> List[Dict[str, object]]:
    """Real, mounted filesystems with room on them — the obvious places to share from."""
    seen = set()
    rows: List[Dict[str, object]] = []
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mount = parts[1].replace("\\040", " ")
                fstype = parts[2]
                if fstype in SKIP_FSTYPES or mount in seen:
                    continue
                if mount.startswith(("/proc", "/sys", "/dev", "/run", "/snap", "/var/lib/docker")):
                    continue
                seen.add(mount)
                usage = disk_usage(mount)
                if not usage["total"]:
                    continue
                rows.append(
                    {
                        "path": mount,
                        "fstype": fstype,
                        "free": usage["free"],
                        "free_h": human_bytes(usage["free"]),
                        "total_h": human_bytes(usage["total"]),
                        "percent": usage["percent"],
                    }
                )
    except OSError:
        pass
    rows.sort(key=lambda row: -int(row["free"]))
    return rows


def suggestions() -> List[Dict[str, object]]:
    """Shortcut buttons for the picker."""
    out: List[Dict[str, object]] = []
    for prefix in INTERESTING_PREFIXES:
        if os.path.isdir(prefix):
            out.append({"path": prefix, "label": prefix})
    for mount in mount_points():
        path = str(mount["path"])
        if path == "/" or any(row["path"] == path for row in out):
            continue
        out.append({"path": path, "label": f"{path} · {mount['free_h']} free"})
    return out[:12]


def create_directory(path: str) -> Dict[str, object]:
    target = os.path.abspath(os.path.expanduser(path))
    if os.path.isdir(target):
        return {"ok": True, "path": target, "message": "Folder already exists."}
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "path": target, "message": f"Could not create {target}: {exc.strerror}"}
    return {"ok": True, "path": target, "message": f"Created {target}."}
