"""Live bandwidth, active connections and usage history.

There are three sources of truth here, in order of preference:

* `ss -tin` gives exact byte counters per TCP socket, so throughput is measured
  rather than guessed, and it is attributable to a single PS2.
* `/proc/<smbd pid>/fd` tells us which file each connection has open and, via
  `fdinfo`, how far through it the console has read — that is what turns
  "something is downloading" into "the PS2 is 42% of the way through Gran
  Turismo 4".
* `/proc/net/tcp` always works and always lists the connections, even when
  iproute2 is missing, so the connection list never goes blank.

Nothing here needs Samba's own tooling, which keeps the readings identical
across distributions and Samba versions.
"""

from __future__ import annotations

import os
import re
import socket
import sqlite3
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from .util import human_bytes, is_root, run, which

SMB_PORTS = (445, 139)
TCP_ESTABLISHED = "01"

IMAGE_HINT = (".iso", ".zso", ".cso", ".bin", ".img", ".vmc")


# ---------------------------------------------------------------------------
# /proc/net/tcp
# ---------------------------------------------------------------------------


def _hex_to_ipv4(value: str) -> str:
    return socket.inet_ntoa(struct.pack("<I", int(value, 16)))


def _hex_to_ipv6(value: str) -> str:
    words = [value[i : i + 8] for i in range(0, 32, 8)]
    packed = b"".join(struct.pack("<I", int(word, 16)) for word in words)
    text = socket.inet_ntop(socket.AF_INET6, packed)
    if text.startswith("::ffff:") and "." in text:
        return text[7:]
    return text


def _parse_addr(token: str) -> Tuple[str, int]:
    addr, _, port = token.partition(":")
    try:
        if len(addr) == 8:
            return (_hex_to_ipv4(addr), int(port, 16))
        if len(addr) == 32:
            return (_hex_to_ipv6(addr), int(port, 16))
    except (ValueError, OSError, struct.error):
        pass
    return ("", 0)


@dataclass
class Connection:
    local_ip: str
    local_port: int
    peer_ip: str
    peer_port: int
    inode: str
    pid: Optional[int] = None

    @property
    def key(self) -> str:
        return f"{self.local_ip}:{self.local_port}-{self.peer_ip}:{self.peer_port}"


def read_tcp_connections(ports: Tuple[int, ...] = SMB_PORTS) -> List[Connection]:
    """Established inbound connections to the SMB ports, from /proc."""
    found: List[Connection] = []
    for proc_file in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(proc_file, "r", encoding="utf-8", errors="replace") as handle:
                next(handle, None)  # header
                for line in handle:
                    fields = line.split()
                    if len(fields) < 10 or fields[3] != TCP_ESTABLISHED:
                        continue
                    local_ip, local_port = _parse_addr(fields[1])
                    if local_port not in ports:
                        continue
                    peer_ip, peer_port = _parse_addr(fields[2])
                    if not peer_ip:
                        continue
                    found.append(
                        Connection(
                            local_ip=local_ip,
                            local_port=local_port,
                            peer_ip=peer_ip,
                            peer_port=peer_port,
                            inode=fields[9],
                        )
                    )
        except (OSError, StopIteration):
            continue
    return found


def read_listening_ports() -> List[int]:
    listening = set()
    for proc_file in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(proc_file, "r", encoding="utf-8", errors="replace") as handle:
                next(handle, None)
                for line in handle:
                    fields = line.split()
                    if len(fields) < 4 or fields[3] != "0A":
                        continue
                    _, port = _parse_addr(fields[1])
                    if port:
                        listening.add(port)
        except (OSError, StopIteration):
            continue
    return sorted(listening)


# ---------------------------------------------------------------------------
# socket byte counters via ss
# ---------------------------------------------------------------------------

SS_SENT_RE = re.compile(r"\bbytes_sent:(\d+)")
SS_ACKED_RE = re.compile(r"\bbytes_acked:(\d+)")
SS_RECV_RE = re.compile(r"\bbytes_received:(\d+)")
SS_ADDR_RE = re.compile(r"(\S+):(\d+)\s+(\S+):(\d+)\s*$")


def parse_ss(output: str) -> Dict[str, Tuple[int, int]]:
    """Map `local-remote` to (bytes_sent, bytes_received) from `ss -tin` output.

    `ss` prints the socket on one line and its TCP info on the following
    indented line, so state carries from one line to the next.
    """
    counters: Dict[str, Tuple[int, int]] = {}
    current: Optional[str] = None
    for line in output.splitlines():
        if not line.strip():
            continue
        if not line[0].isspace():
            match = SS_ADDR_RE.search(line.strip())
            current = None
            if match:
                local_ip = match.group(1).strip("[]")
                peer_ip = match.group(3).strip("[]")
                if local_ip.startswith("::ffff:"):
                    local_ip = local_ip[7:]
                if peer_ip.startswith("::ffff:"):
                    peer_ip = peer_ip[7:]
                current = f"{local_ip}:{match.group(2)}-{peer_ip}:{match.group(4)}"
                counters.setdefault(current, (0, 0))
            continue
        if current is None:
            continue
        sent_match = SS_SENT_RE.search(line) or SS_ACKED_RE.search(line)
        recv_match = SS_RECV_RE.search(line)
        sent = int(sent_match.group(1)) if sent_match else 0
        received = int(recv_match.group(1)) if recv_match else 0
        counters[current] = (sent, received)
    return counters


def ss_counters() -> Optional[Dict[str, Tuple[int, int]]]:
    if not which("ss"):
        return None
    ports = " or ".join(f"sport = :{port}" for port in SMB_PORTS)
    result = run(["ss", "-tinH", "state", "established", f"( {ports} )"], timeout=10)
    if not result.ok:
        return None
    return parse_ss(result.out)


# ---------------------------------------------------------------------------
# smbd processes: which file is each console reading?
# ---------------------------------------------------------------------------


@dataclass
class OpenFile:
    path: str
    size: int
    position: int

    @property
    def percent(self) -> float:
        if self.size <= 0:
            return 0.0
        return min(100.0, self.position / self.size * 100.0)


def is_smbd_name(comm: str) -> bool:
    """Samba renames the process that serves each client.

    The parent is plain `smbd`, but every forked child becomes
    `smbd[192.168.1.50]` — truncated to 15 characters by the kernel — and it
    is the child that holds the open disc image. Matching only `smbd` would
    find the one process that never has a game file open.
    """
    return comm.startswith("smbd")


def smbd_pids() -> List[int]:
    pids = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return pids
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/comm", "r", encoding="utf-8", errors="replace") as handle:
                if is_smbd_name(handle.read().strip()):
                    pids.append(int(entry))
        except OSError:
            continue
    return pids


def _fd_position(pid: int, fd: str) -> int:
    try:
        with open(f"/proc/{pid}/fdinfo/{fd}", "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("pos:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def inspect_smbd(share_path: str = "") -> Dict[str, dict]:
    """Map socket inode -> {pid, files} for every live smbd child.

    Requires root to read other processes' fd tables; without it the caller
    simply gets an empty map and falls back to connection-level data.
    """
    by_inode: Dict[str, dict] = {}
    if not is_root():
        return by_inode
    share_real = os.path.realpath(share_path) if share_path else ""
    for pid in smbd_pids():
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        inodes: List[str] = []
        files: List[OpenFile] = []
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if target.startswith("socket:["):
                inodes.append(target[8:-1])
                continue
            if not target.startswith("/"):
                continue
            if share_real and not (target == share_real or target.startswith(share_real.rstrip("/") + "/")):
                continue
            try:
                info = os.stat(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if not info.st_size:
                continue
            files.append(OpenFile(path=target, size=info.st_size, position=_fd_position(pid, fd)))
        files.sort(key=lambda f: -f.size)
        for inode in inodes:
            by_inode[inode] = {"pid": pid, "files": files}
    return by_inode


# ---------------------------------------------------------------------------
# usage history
# ---------------------------------------------------------------------------


class UsageStore:
    """Per-minute traffic buckets in SQLite, so usage survives a restart."""

    def __init__(self, path: str, retention_days: int = 30):
        self.path = path
        self.retention_days = retention_days
        self._lock = threading.Lock()
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS traffic (
                   minute INTEGER NOT NULL,
                   client TEXT NOT NULL,
                   tx INTEGER NOT NULL DEFAULT 0,
                   rx INTEGER NOT NULL DEFAULT 0,
                   PRIMARY KEY (minute, client))"""
        )
        self._db.commit()
        self._last_prune = 0.0

    def close(self) -> None:
        with self._lock:
            try:
                self._db.close()
            except sqlite3.Error:
                pass

    def record(self, when: float, per_client: Dict[str, Tuple[int, int]]) -> None:
        if not per_client:
            return
        minute = int(when // 60 * 60)
        rows = [(minute, client, tx, rx) for client, (tx, rx) in per_client.items() if tx or rx]
        if not rows:
            return
        with self._lock:
            self._db.executemany(
                """INSERT INTO traffic (minute, client, tx, rx) VALUES (?, ?, ?, ?)
                   ON CONFLICT(minute, client) DO UPDATE SET tx = tx + excluded.tx, rx = rx + excluded.rx""",
                rows,
            )
            self._db.commit()
        self.maybe_prune()

    def maybe_prune(self) -> None:
        now = time.time()
        if now - self._last_prune < 3600:
            return
        self._last_prune = now
        cutoff = int(now - self.retention_days * 86400)
        with self._lock:
            self._db.execute("DELETE FROM traffic WHERE minute < ?", (cutoff,))
            self._db.commit()

    def totals(self, since: float) -> Tuple[int, int]:
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(SUM(tx), 0), COALESCE(SUM(rx), 0) FROM traffic WHERE minute >= ?",
                (int(since),),
            ).fetchone()
        return (int(row[0]), int(row[1]))

    def per_client(self, since: float) -> List[dict]:
        with self._lock:
            rows = self._db.execute(
                """SELECT client, SUM(tx), SUM(rx), MAX(minute) FROM traffic
                   WHERE minute >= ? GROUP BY client ORDER BY SUM(tx) + SUM(rx) DESC""",
                (int(since),),
            ).fetchall()
        return [
            {
                "client": row[0],
                "tx": int(row[1]),
                "rx": int(row[2]),
                "total": int(row[1]) + int(row[2]),
                "last_seen": int(row[3]),
            }
            for row in rows
        ]

    def daily(self, days: int = 14) -> List[dict]:
        """Totals per local calendar day, oldest first, with empty days filled in."""
        since = time.time() - days * 86400
        with self._lock:
            rows = self._db.execute(
                "SELECT minute, tx, rx FROM traffic WHERE minute >= ?", (int(since),)
            ).fetchall()
        buckets: Dict[str, List[int]] = {}
        for minute, tx, rx in rows:
            day = time.strftime("%Y-%m-%d", time.localtime(minute))
            slot = buckets.setdefault(day, [0, 0])
            slot[0] += int(tx)
            slot[1] += int(rx)
        out = []
        today = time.time()
        for offset in range(days - 1, -1, -1):
            day = time.strftime("%Y-%m-%d", time.localtime(today - offset * 86400))
            tx, rx = buckets.get(day, [0, 0])
            out.append({"day": day, "tx": tx, "rx": rx, "total": tx + rx})
        return out


# ---------------------------------------------------------------------------
# the sampler
# ---------------------------------------------------------------------------


@dataclass
class ClientState:
    ip: str
    first_seen: float
    last_seen: float
    tx_rate: float = 0.0
    rx_rate: float = 0.0
    tx_total: int = 0
    rx_total: int = 0
    connections: int = 0
    files: List[dict] = field(default_factory=list)
    peak_rate: float = 0.0


class Monitor:
    """Background sampler. Start once, read `snapshot()` as often as you like."""

    def __init__(self, store: UsageStore, *, interval: float = 1.0, history_seconds: int = 300):
        self.store = store
        self.interval = max(0.5, interval)
        self.history: Deque[Tuple[float, float, float]] = deque(
            maxlen=max(30, int(history_seconds / self.interval))
        )
        self.share_path = ""
        self.source = "none"
        self._clients: Dict[str, ClientState] = {}
        self._previous: Dict[str, Tuple[int, int]] = {}
        self._previous_positions: Dict[str, int] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._session_tx = 0
        self._session_rx = 0
        self._started = time.time()
        self._primed = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ps2share-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.time()
            try:
                self.sample()
            except Exception:  # pragma: no cover - a sampler must never die
                pass
            elapsed = time.time() - started
            self._stop.wait(max(0.1, self.interval - elapsed))

    # -- sampling ----------------------------------------------------------

    def sample(self) -> None:
        now = time.time()
        connections = read_tcp_connections()
        smbd = inspect_smbd(self.share_path)

        counters = ss_counters()
        deltas: Dict[str, Tuple[int, int]] = {}

        if counters is not None:
            self.source = "ss"
            seen_keys = set()
            for conn in connections:
                key = conn.key
                seen_keys.add(key)
                current = counters.get(key)
                if current is None:
                    continue
                previous = self._previous.get(key)
                if previous is None:
                    # A socket we have not seen before opened since the last
                    # sample, so everything on its counters is new traffic.
                    # The exception is the very first sample, where a transfer
                    # already in flight would otherwise land in one spike.
                    sent_delta, recv_delta = (0, 0) if not self._primed else current
                else:
                    # A reused address pair restarts its counters at zero.
                    sent_delta = current[0] - previous[0] if current[0] >= previous[0] else current[0]
                    recv_delta = current[1] - previous[1] if current[1] >= previous[1] else current[1]
                self._previous[key] = current
                if sent_delta or recv_delta:
                    tx, rx = deltas.get(conn.peer_ip, (0, 0))
                    deltas[conn.peer_ip] = (tx + sent_delta, rx + recv_delta)
            for stale in set(self._previous) - seen_keys:
                self._previous.pop(stale, None)
        else:
            # Without iproute2, read progress through the open files is the
            # best available proxy for throughput.
            self.source = "fdinfo" if smbd else "none"
            for conn in connections:
                entry = smbd.get(conn.inode)
                if not entry:
                    continue
                for open_file in entry["files"]:
                    key = f"{entry['pid']}:{open_file.path}"
                    previous = self._previous_positions.get(key)
                    self._previous_positions[key] = open_file.position
                    if previous is None or open_file.position < previous:
                        continue
                    moved = open_file.position - previous
                    if moved:
                        tx, rx = deltas.get(conn.peer_ip, (0, 0))
                        deltas[conn.peer_ip] = (tx + moved, rx)

        self._primed = True
        self._update_clients(now, connections, smbd, deltas)
        self.store.record(now, {ip: (int(tx), int(rx)) for ip, (tx, rx) in deltas.items()})

    def _update_clients(
        self,
        now: float,
        connections: List[Connection],
        smbd: Dict[str, dict],
        deltas: Dict[str, Tuple[int, int]],
    ) -> None:
        per_ip_connections: Dict[str, int] = {}
        per_ip_files: Dict[str, List[dict]] = {}
        for conn in connections:
            per_ip_connections[conn.peer_ip] = per_ip_connections.get(conn.peer_ip, 0) + 1
            entry = smbd.get(conn.inode)
            if not entry:
                continue
            bucket = per_ip_files.setdefault(conn.peer_ip, [])
            for open_file in entry["files"]:
                if any(existing["path"] == open_file.path for existing in bucket):
                    continue
                bucket.append(
                    {
                        "path": open_file.path,
                        "name": os.path.basename(open_file.path),
                        "size": open_file.size,
                        "size_h": human_bytes(open_file.size),
                        "position": open_file.position,
                        "percent": round(open_file.percent, 1),
                        # Samba reads with pread()/sendfile() and explicit
                        # offsets, which never move the descriptor, so a
                        # position of zero means "not reported" rather than
                        # "start of file". Only claim progress when it is real.
                        "has_position": open_file.position > 0,
                        "is_image": open_file.path.lower().endswith(IMAGE_HINT),
                    }
                )

        total_tx = total_rx = 0.0
        with self._lock:
            for ip, count in per_ip_connections.items():
                state = self._clients.get(ip)
                if state is None:
                    state = ClientState(ip=ip, first_seen=now, last_seen=now)
                    self._clients[ip] = state
                tx_delta, rx_delta = deltas.get(ip, (0, 0))
                state.tx_rate = tx_delta / self.interval
                state.rx_rate = rx_delta / self.interval
                state.peak_rate = max(state.peak_rate, state.tx_rate + state.rx_rate)
                state.tx_total += int(tx_delta)
                state.rx_total += int(rx_delta)
                state.connections = count
                state.files = per_ip_files.get(ip, [])
                state.last_seen = now
                total_tx += state.tx_rate
                total_rx += state.rx_rate

            # Connections that went away stop counting towards the live rate
            # but stay listed briefly so a finished transfer does not just vanish.
            for ip, state in list(self._clients.items()):
                if ip in per_ip_connections:
                    continue
                state.tx_rate = 0.0
                state.rx_rate = 0.0
                state.connections = 0
                state.files = []
                if now - state.last_seen > 300:
                    self._clients.pop(ip, None)

            self._session_tx += int(sum(d[0] for d in deltas.values()))
            self._session_rx += int(sum(d[1] for d in deltas.values()))
            self.history.append((now, total_tx, total_rx))

    # -- reading -----------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            history = list(self.history)
            clients = [
                {
                    "ip": state.ip,
                    "tx_rate": round(state.tx_rate, 1),
                    "rx_rate": round(state.rx_rate, 1),
                    "rate_h": human_bytes(state.tx_rate + state.rx_rate, rate=True),
                    "tx_total": state.tx_total,
                    "rx_total": state.rx_total,
                    "total_h": human_bytes(state.tx_total + state.rx_total),
                    "peak_rate": round(state.peak_rate, 1),
                    "peak_h": human_bytes(state.peak_rate, rate=True),
                    "connections": state.connections,
                    "active": state.connections > 0,
                    "since": state.first_seen,
                    "duration": now_minus(state.first_seen),
                    "last_seen": state.last_seen,
                    "files": state.files,
                }
                for state in sorted(
                    self._clients.values(),
                    key=lambda c: (-c.connections, -(c.tx_rate + c.rx_rate), c.ip),
                )
            ]
            session_tx, session_rx = self._session_tx, self._session_rx

        current_tx = history[-1][1] if history else 0.0
        current_rx = history[-1][2] if history else 0.0
        return {
            "ts": time.time(),
            "source": self.source,
            "uptime": now_minus(self._started),
            "current": {
                "tx": round(current_tx, 1),
                "rx": round(current_rx, 1),
                "total": round(current_tx + current_rx, 1),
                "total_h": human_bytes(current_tx + current_rx, rate=True),
            },
            "session": {
                "tx": session_tx,
                "rx": session_rx,
                "total_h": human_bytes(session_tx + session_rx),
            },
            "clients": clients,
            "active_clients": sum(1 for client in clients if client["active"]),
            "history": [[round(ts, 1), round(tx, 1), round(rx, 1)] for ts, tx, rx in history],
        }


def now_minus(timestamp: float) -> float:
    return max(0.0, time.time() - timestamp)
