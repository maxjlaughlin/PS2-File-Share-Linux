"""The Open PS2 Loader side of the share: folder layout, game scanning, checks.

OPL expects a very specific set of folders at the root of the share.  It will
happily connect without them, but game lists, artwork, per-game settings and
virtual memory cards all silently do nothing until the folders exist, which is
the single most common "my PS2 sees the share but there are no games" problem.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import struct
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .util import disk_usage, filesystem_of, human_bytes

SECTOR = 2048

# The folders OPL looks for, and why each one matters.
OPL_FOLDERS: Dict[str, str] = {
    "CD": "CD-sized disc images (most PS1-era-small PS2 titles)",
    "DVD": "DVD-sized disc images — where nearly every game goes",
    "VMC": "Virtual memory cards, so saves never touch a real card",
    "CFG": "Per-game settings written by OPL",
    "ART": "Box art and screenshots shown in the game list",
    "THM": "Themes for the OPL interface",
    "LNG": "Translation files",
    "APPS": "Homebrew ELF applications",
}

IMAGE_EXTENSIONS = (".iso", ".zso", ".cso")
SPLIT_RE = re.compile(r"^(?P<stem>.+\.iso)\.(?P<part>\d{2})$", re.IGNORECASE)
GAME_ID_RE = re.compile(r"\b([A-Z]{4})[_\-]?(\d{3})\.?(\d{2})\b")
BOOT_RE = re.compile(r"BOOT2?\s*=\s*cdrom0?:\\?\.?\\?([^\s;]+)", re.IGNORECASE)

# OPL truncates names longer than this in its game list.
MAX_NAME_LENGTH = 32
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9 ._\-&'!(),\[\]+]+$")

# Filesystems that will bite a PS2 library.
LIMITED_FILESYSTEMS = {
    "vfat": "FAT32 cannot hold a file larger than 4 GB — most DVD images will not fit.",
    "msdos": "FAT cannot hold a file larger than 4 GB.",
    "exfat": "exFAT works but is slower than ext4 and has no unix permissions.",
    "ntfs": "NTFS via FUSE is noticeably slower than a native Linux filesystem.",
    "ntfs3": "NTFS is usable but slower than a native Linux filesystem.",
    "fuseblk": "A FUSE-mounted filesystem is usually much slower than ext4/xfs/btrfs.",
}


@dataclass
class Game:
    name: str
    path: str
    relpath: str
    folder: str
    size: int
    kind: str  # iso | zso | cso | split
    parts: int = 1
    game_id: str = ""
    volume: str = ""
    warnings: List[str] = field(default_factory=list)
    has_art: bool = False

    def to_dict(self) -> dict:
        data = {
            "name": self.name,
            "path": self.path,
            "relpath": self.relpath,
            "folder": self.folder,
            "size": self.size,
            "size_h": human_bytes(self.size),
            "kind": self.kind,
            "parts": self.parts,
            "game_id": self.game_id,
            "volume": self.volume,
            "warnings": self.warnings,
            "has_art": self.has_art,
        }
        return data


# ---------------------------------------------------------------------------
# folder layout
# ---------------------------------------------------------------------------


def layout_state(share_path: str) -> List[dict]:
    rows = []
    for name, purpose in OPL_FOLDERS.items():
        full = os.path.join(share_path, name)
        rows.append(
            {
                "name": name,
                "purpose": purpose,
                "exists": os.path.isdir(full),
                "writable": os.access(full, os.W_OK) if os.path.isdir(full) else False,
            }
        )
    return rows


def ensure_layout(share_path: str, owner_uid: Optional[int] = None, owner_gid: Optional[int] = None) -> List[str]:
    """Create any missing OPL folders. Returns the names that were created."""
    created = []
    for name in OPL_FOLDERS:
        full = os.path.join(share_path, name)
        if os.path.isdir(full):
            continue
        os.makedirs(full, exist_ok=True)
        created.append(name)
        if owner_uid is not None and owner_gid is not None:
            try:
                os.chown(full, owner_uid, owner_gid)
            except OSError:
                pass
        try:
            os.chmod(full, 0o775)
        except OSError:
            pass
    return created


def fix_permissions(share_path: str, uid: int, gid: int) -> Tuple[int, List[str]]:
    """Make the whole share readable and writable by the account Samba runs as."""
    changed = 0
    errors: List[str] = []
    for root, dirs, files in os.walk(share_path):
        for target, is_dir in [(root, True)] + [(os.path.join(root, f), False) for f in files]:
            try:
                info = os.lstat(target)
                if os.path.islink(target):
                    continue
                if info.st_uid != uid or info.st_gid != gid:
                    os.chown(target, uid, gid)
                    changed += 1
                mode = 0o775 if is_dir else 0o664
                if (info.st_mode & 0o777) != mode:
                    os.chmod(target, mode)
                    changed += 1
            except OSError as exc:
                if len(errors) < 10:
                    errors.append(f"{target}: {exc.strerror}")
    return changed, errors


# ---------------------------------------------------------------------------
# reading the game id out of a disc image
# ---------------------------------------------------------------------------


def _iso_find_file(handle, filename: str = "SYSTEM.CNF") -> Optional[Tuple[int, int]]:
    """Locate a file in the ISO9660 root directory. Returns (lba, size)."""
    handle.seek(16 * SECTOR)
    pvd = handle.read(SECTOR)
    if len(pvd) < 190 or pvd[1:6] != b"CD001":
        return None
    root_record = pvd[156:190]
    extent = struct.unpack_from("<I", root_record, 2)[0]
    length = struct.unpack_from("<I", root_record, 10)[0]
    if not extent or not length or length > 4 * 1024 * 1024:
        return None

    handle.seek(extent * SECTOR)
    directory = handle.read(length)
    wanted = filename.upper().encode("ascii")
    pos = 0
    while pos < len(directory):
        rec_len = directory[pos]
        if rec_len == 0:
            # Records never straddle a sector; jump to the next one.
            pos = (pos // SECTOR + 1) * SECTOR
            continue
        if pos + rec_len > len(directory):
            break
        name_len = directory[pos + 32]
        name = directory[pos + 33 : pos + 33 + name_len].upper()
        if name.split(b";")[0] == wanted:
            lba = struct.unpack_from("<I", directory, pos + 2)[0]
            size = struct.unpack_from("<I", directory, pos + 10)[0]
            return (lba, size)
        pos += rec_len
    return None


def _iso_volume_label(handle) -> str:
    handle.seek(16 * SECTOR)
    pvd = handle.read(SECTOR)
    if len(pvd) < 72 or pvd[1:6] != b"CD001":
        return ""
    return pvd[40:72].decode("latin-1").strip()


def _normalise_game_id(raw: str) -> str:
    match = GAME_ID_RE.search(raw.upper().replace("\\", "").replace("/", ""))
    if not match:
        return ""
    return f"{match.group(1)}_{match.group(2)}.{match.group(3)}"


def read_iso_metadata(path: str) -> Tuple[str, str]:
    """Return (game_id, volume_label) for a plain .iso, best effort."""
    try:
        with open(path, "rb") as handle:
            volume = _iso_volume_label(handle)
            located = _iso_find_file(handle)
            if not located:
                return ("", volume)
            lba, size = located
            if size <= 0 or size > 64 * 1024:
                return ("", volume)
            handle.seek(lba * SECTOR)
            system_cnf = handle.read(size).decode("latin-1", "replace")
    except OSError:
        return ("", "")
    boot = BOOT_RE.search(system_cnf)
    if boot:
        return (_normalise_game_id(boot.group(1)), volume)
    return (_normalise_game_id(system_cnf), volume)


class _CsoReader:
    """Minimal read-only view of a CISO image, enough to reach SYSTEM.CNF.

    ZSO uses LZ4 and so is skipped — there is no LZ4 in the standard library
    and the app refuses to grow a dependency for a cosmetic feature.
    """

    def __init__(self, path: str):
        self._handle = open(path, "rb")
        header = self._handle.read(24)
        if len(header) < 24 or header[:4] != b"CISO":
            self._handle.close()
            raise ValueError("not a CISO image")
        (_, _header_size, self.total_bytes, self.block_size, _ver, self.align, _res) = struct.unpack(
            "<4sIQIBBH", header
        )
        if not self.block_size:
            self._handle.close()
            raise ValueError("bad CISO block size")
        self.total_blocks = self.total_bytes // self.block_size
        if self.total_blocks <= 0 or self.total_blocks > 8_000_000:
            self._handle.close()
            raise ValueError("implausible CISO index")
        raw_index = self._handle.read(4 * (self.total_blocks + 1))
        self._index = struct.unpack(f"<{len(raw_index) // 4}I", raw_index)
        self._pos = 0

    def close(self) -> None:
        self._handle.close()

    def seek(self, offset: int) -> None:
        self._pos = offset

    def _block(self, number: int) -> bytes:
        if number + 1 >= len(self._index):
            return b""
        start = self._index[number]
        end = self._index[number + 1]
        plain = bool(start & 0x80000000)
        start = (start & 0x7FFFFFFF) << self.align
        end = (end & 0x7FFFFFFF) << self.align
        if end <= start:
            return b""
        self._handle.seek(start)
        raw = self._handle.read(end - start)
        if plain:
            return raw[: self.block_size]
        try:
            return zlib.decompressobj(-15).decompress(raw, self.block_size)
        except zlib.error:
            return b""

    def read(self, length: int) -> bytes:
        out = bytearray()
        while len(out) < length:
            block_no, inner = divmod(self._pos + len(out), self.block_size)
            chunk = self._block(block_no)
            if not chunk:
                break
            out += chunk[inner : inner + (length - len(out))]
            if len(chunk) <= inner:
                break
        self._pos += len(out)
        return bytes(out)


def read_cso_metadata(path: str) -> Tuple[str, str]:
    try:
        reader = _CsoReader(path)
    except (OSError, ValueError, struct.error):
        return ("", "")
    try:
        volume = _iso_volume_label(reader)
        located = _iso_find_file(reader)
        if not located:
            return ("", volume)
        lba, size = located
        if size <= 0 or size > 64 * 1024:
            return ("", volume)
        reader.seek(lba * SECTOR)
        system_cnf = reader.read(size).decode("latin-1", "replace")
    except (OSError, struct.error):
        return ("", "")
    finally:
        reader.close()
    boot = BOOT_RE.search(system_cnf)
    if boot:
        return (_normalise_game_id(boot.group(1)), volume)
    return ("", volume)


def read_metadata(path: str, kind: str) -> Tuple[str, str]:
    if kind == "cso":
        return read_cso_metadata(path)
    if kind == "zso":
        return ("", "")
    return read_iso_metadata(path)


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------


def name_warnings(name: str) -> List[str]:
    warnings = []
    stem = name
    for ext in IMAGE_EXTENSIONS:
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break
    if len(stem) > MAX_NAME_LENGTH:
        warnings.append(f"Name is {len(stem)} characters — OPL shows the first {MAX_NAME_LENGTH}.")
    if not SAFE_NAME_RE.match(stem):
        warnings.append("Name has characters OPL may not render; stick to letters, digits, spaces and . _ - ( )")
    return warnings


class GameScanner:
    """Scans CD/ and DVD/ and caches the expensive part (reading game ids)."""

    def __init__(self, cache_path: str = ""):
        self.cache_path = cache_path
        self._cache: Dict[str, dict] = {}
        self._load_cache()

    def _load_cache(self) -> None:
        if not self.cache_path:
            return
        try:
            with open(self.cache_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self._cache = data
        except (OSError, ValueError):
            self._cache = {}

    def _save_cache(self) -> None:
        if not self.cache_path:
            return
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self._cache, handle)
            os.replace(tmp, self.cache_path)
        except OSError:
            pass

    def _metadata(self, path: str, size: int, mtime: float, kind: str) -> Tuple[str, str]:
        key = f"{path}:{size}:{int(mtime)}"
        cached = self._cache.get(key)
        if cached:
            return (cached.get("id", ""), cached.get("volume", ""))
        game_id, volume = read_metadata(path, kind)
        self._cache[key] = {"id": game_id, "volume": volume}
        return (game_id, volume)

    def scan(self, share_path: str) -> List[Game]:
        if not share_path or not os.path.isdir(share_path):
            return []
        art = self._art_index(os.path.join(share_path, "ART"))
        games: List[Game] = []
        for folder in ("CD", "DVD"):
            directory = os.path.join(share_path, folder)
            if not os.path.isdir(directory):
                continue
            games.extend(self._scan_folder(directory, folder, share_path, art))
        self._save_cache()
        games.sort(key=lambda g: g.name.lower())
        return games

    def _art_index(self, art_dir: str) -> set:
        found = set()
        try:
            for entry in os.scandir(art_dir):
                if entry.is_file():
                    found.add(entry.name.split("_COV")[0].split(".")[0].upper())
        except OSError:
            pass
        return found

    def _scan_folder(self, directory: str, folder: str, share_path: str, art: set) -> List[Game]:
        try:
            entries = [e for e in os.scandir(directory) if e.is_file()]
        except OSError:
            return []

        splits: Dict[str, List[os.DirEntry]] = {}
        singles: List[os.DirEntry] = []
        for entry in entries:
            match = SPLIT_RE.match(entry.name)
            if match:
                splits.setdefault(match.group("stem"), []).append(entry)
            elif entry.name.lower().endswith(IMAGE_EXTENSIONS):
                singles.append(entry)

        games: List[Game] = []
        for entry in singles:
            try:
                info = entry.stat()
            except OSError:
                continue
            kind = entry.name.rsplit(".", 1)[-1].lower()
            game_id, volume = self._metadata(entry.path, info.st_size, info.st_mtime, kind)
            game = Game(
                name=entry.name,
                path=entry.path,
                relpath=os.path.relpath(entry.path, share_path),
                folder=folder,
                size=info.st_size,
                kind=kind,
                game_id=game_id,
                volume=volume,
                warnings=name_warnings(entry.name),
            )
            if kind == "zso":
                game.warnings.append("ZSO images need an OPL build with ZSO support (1.2.0 or newer).")
            game.has_art = bool(game_id) and game_id.replace("_", "").replace(".", "").upper() in {
                a.replace("_", "").replace(".", "").upper() for a in art
            }
            games.append(game)

        for stem, parts in splits.items():
            total = 0
            for part in parts:
                try:
                    total += part.stat().st_size
                except OSError:
                    pass
            games.append(
                Game(
                    name=stem,
                    path=os.path.join(directory, stem),
                    relpath=os.path.relpath(os.path.join(directory, stem), share_path),
                    folder=folder,
                    size=total,
                    kind="split",
                    parts=len(parts),
                    warnings=name_warnings(stem)
                    + ["Split image — only needed on FAT32. On ext4 you can join the parts back together."],
                )
            )
        return games


def list_memory_cards(share_path: str) -> List[dict]:
    directory = os.path.join(share_path, "VMC")
    cards = []
    try:
        for entry in sorted(os.scandir(directory), key=lambda e: e.name):
            if not entry.is_file():
                continue
            size = entry.stat().st_size
            cards.append(
                {
                    "name": entry.name,
                    "size": size,
                    "size_h": human_bytes(size),
                    "slots": round(size / (8 * 1024 * 1024), 1) if size else 0,
                }
            )
    except OSError:
        pass
    return cards


def _permitted(info: os.stat_result, uid: int, groups: set, bit: int) -> bool:
    """Does `uid` hold permission `bit` (4=read, 2=write, 1=execute) on this inode?"""
    if uid == 0:
        return True
    if uid == info.st_uid:
        granted = (info.st_mode >> 6) & 7
    elif info.st_gid in groups:
        granted = (info.st_mode >> 3) & 7
    else:
        granted = info.st_mode & 7
    return bool(granted & bit)


def access_problems(share_path: str, username: str, writable: bool = True) -> List[str]:
    """Check that `username` can actually reach and use the share.

    Samba serves the share as this account, and a folder nobody can `cd` into
    produces a connection that succeeds and then fails on every listing — the
    "OPL connects but the game list is empty" case.  One un-traversable parent
    directory anywhere above the share is enough to cause it, which is easy to
    miss because the share folder's own permissions look fine.
    """
    if not share_path or not username:
        return []
    try:
        entry = pwd.getpwnam(username)
    except KeyError:
        return [f"There is no Linux account called '{username}' for Samba to serve the share as."]

    uid, gid = entry.pw_uid, entry.pw_gid
    if uid == 0:
        return []
    try:
        groups = set(os.getgrouplist(username, gid))
    except (OSError, KeyError, AttributeError):
        groups = {gid}

    problems: List[str] = []
    real = os.path.realpath(share_path)
    walked = "/"
    for part in real.split("/"):
        if not part:
            continue
        walked = os.path.join(walked, part)
        try:
            info = os.stat(walked)
        except OSError:
            continue
        if not _permitted(info, uid, groups, 1):
            problems.append(
                f"'{username}' cannot pass through {walked} (mode {info.st_mode & 0o777:04o}), "
                f"so the share will connect but list nothing. Fix with: chmod o+x {walked}"
            )
            break  # everything deeper is unreachable anyway; one fix at a time

    if not problems:
        info = os.stat(real)
        if not _permitted(info, uid, groups, 4):
            problems.append(f"'{username}' cannot read {real}. Press 'Fix permissions'.")
        elif writable and not _permitted(info, uid, groups, 2):
            problems.append(
                f"'{username}' cannot write to {real}, so virtual memory cards and OPL's "
                "own settings will fail. Press 'Fix permissions'."
            )
    return problems


def share_health(share_path: str, serve_as: str = "", writable: bool = True) -> dict:
    """Everything the UI needs to tell the user whether the folder is usable."""
    if not share_path:
        return {"ok": False, "problems": ["No folder chosen yet."], "notes": [], "folders": [], "disk": {}}

    problems: List[str] = []
    notes: List[str] = []

    if not os.path.isdir(share_path):
        problems.append(f"{share_path} does not exist.")
        return {"ok": False, "problems": problems, "notes": notes, "folders": [], "disk": {}}

    problems.extend(access_problems(share_path, serve_as, writable))

    mount, fstype = filesystem_of(share_path)
    if fstype in LIMITED_FILESYSTEMS:
        notes.append(f"{mount} is {fstype}: {LIMITED_FILESYSTEMS[fstype]}")

    folders = layout_state(share_path)
    missing = [row["name"] for row in folders if not row["exists"]]
    if missing:
        problems.append("Missing OPL folders: " + ", ".join(missing))

    usage = disk_usage(share_path)
    if usage["total"] and usage["free"] < 1024 ** 3:
        notes.append("Less than 1 GB free on this filesystem.")

    return {
        "ok": not problems,
        "problems": problems,
        "notes": notes,
        "folders": folders,
        "disk": usage,
        "fstype": fstype,
        "mount": mount,
    }
