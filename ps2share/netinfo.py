"""Working out what address to type into OPL, and whether the firewall is in the way."""

from __future__ import annotations

import ipaddress
import re
import socket
from typing import Dict, List, Optional

from .monitor import read_listening_ports
from .util import run, which


def primary_ip() -> str:
    """The address a device on the LAN would reach this server on.

    Opening a UDP socket toward a public address makes the kernel pick the
    outbound interface; nothing is actually sent.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("198.51.100.1", 9))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def interfaces() -> List[Dict[str, object]]:
    """Every IPv4 address on the box, with the likely-LAN one marked."""
    found: List[Dict[str, object]] = []
    best = primary_ip()

    if which("ip"):
        result = run(["ip", "-o", "-4", "addr", "show"], timeout=10)
        if result.ok:
            for line in result.out.splitlines():
                match = re.search(r"^\d+:\s+(\S+)\s+inet\s+([0-9.]+)/(\d+)", line)
                if not match:
                    continue
                name, address, prefix = match.group(1), match.group(2), int(match.group(3))
                if name == "lo":
                    continue
                found.append(
                    {
                        "name": name,
                        "address": address,
                        "prefix": prefix,
                        "network": _network(address, prefix),
                        "primary": address == best,
                        "private": _is_private(address),
                    }
                )

    if not found:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                address = info[4][0]
                if address.startswith("127."):
                    continue
                if any(row["address"] == address for row in found):
                    continue
                found.append(
                    {
                        "name": "",
                        "address": address,
                        "prefix": 24,
                        "network": _network(address, 24),
                        "primary": address == best,
                        "private": _is_private(address),
                    }
                )
        except socket.gaierror:
            pass

    if best and not any(row["address"] == best for row in found):
        found.insert(
            0,
            {
                "name": "",
                "address": best,
                "prefix": 24,
                "network": _network(best, 24),
                "primary": True,
                "private": _is_private(best),
            },
        )

    found.sort(key=lambda row: (not row["primary"], not row["private"], row["address"]))
    return found


def _network(address: str, prefix: int) -> str:
    try:
        return str(ipaddress.ip_network(f"{address}/{prefix}", strict=False))
    except ValueError:
        return ""


def _is_private(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_private
    except ValueError:
        return False


def port_open_locally(port: int) -> bool:
    return port in read_listening_ports()


def probe_port(host: str, port: int, timeout: float = 1.0) -> bool:
    """Connect to a port the way a PS2 would, from this machine."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# firewalls
# ---------------------------------------------------------------------------


def firewall_state() -> Dict[str, object]:
    """Detect the active firewall and whether SMB looks allowed."""
    if which("ufw"):
        result = run(["ufw", "status"], timeout=15)
        if result.ok:
            active = "Status: active" in result.out
            allowed = bool(re.search(r"^(445|139|Samba)\b.*ALLOW", result.out, re.MULTILINE))
            return {
                "tool": "ufw",
                "active": active,
                "allows_smb": allowed or not active,
                "detail": result.out.strip()[:2000],
            }
    if which("firewall-cmd"):
        state = run(["firewall-cmd", "--state"], timeout=15)
        if state.text.strip() == "running":
            services = run(["firewall-cmd", "--list-services"], timeout=15)
            ports = run(["firewall-cmd", "--list-ports"], timeout=15)
            allowed = "samba" in services.out or "445" in ports.out
            return {
                "tool": "firewalld",
                "active": True,
                "allows_smb": allowed,
                "detail": (services.text + " " + ports.text).strip()[:2000],
            }
    if which("nft"):
        result = run(["nft", "list", "ruleset"], timeout=15)
        if result.ok and result.out.strip():
            policy_drop = bool(re.search(r"type filter hook input.*policy drop", result.out))
            allowed = "445" in result.out or not policy_drop
            return {
                "tool": "nftables",
                "active": policy_drop,
                "allows_smb": allowed,
                "detail": "nftables ruleset present",
            }
    return {"tool": "", "active": False, "allows_smb": True, "detail": "No managed firewall detected."}


def firewall_commands(source_network: str = "") -> List[str]:
    """The exact commands to open SMB — shown to the user before anything runs."""
    state = firewall_state()
    scope = source_network or "192.168.0.0/16"
    if state["tool"] == "ufw":
        return [f"ufw allow from {scope} to any app Samba"]
    if state["tool"] == "firewalld":
        return ["firewall-cmd --permanent --add-service=samba", "firewall-cmd --reload"]
    if state["tool"] == "nftables":
        return [
            "nft add rule inet filter input ip saddr " + scope + " tcp dport {445, 139} accept",
        ]
    return []


def open_firewall(source_network: str = "") -> Dict[str, object]:
    commands = firewall_commands(source_network)
    if not commands:
        return {"ok": True, "message": "No managed firewall to change.", "ran": []}
    ran = []
    for command in commands:
        result = run(command.split(), timeout=30)
        ran.append({"command": command, "ok": result.ok, "output": result.text[:500]})
        if not result.ok:
            return {"ok": False, "message": f"`{command}` failed.", "ran": ran}
    return {"ok": True, "message": "Firewall updated.", "ran": ran}


def opl_settings(share_name: str, access_mode: str, username: str) -> Dict[str, object]:
    """The literal values to type into OPL's Network Settings screen."""
    address = primary_ip()
    return {
        "ps2_ip": "Auto (DHCP) — or a static address on the same subnet",
        "server_ip": address,
        "server_port": 445,
        "share_name": share_name,
        "user": username if access_mode == "user" else "guest",
        "password": "(your SMB password)" if access_mode == "user" else "(leave blank)",
        "address_type": "IP",
        "path_type": "Share",
        "smb_version": "Auto / SMB1",
        "steps": [
            "On the PS2: Open PS2 Loader → Settings → Network Settings.",
            "Set PS2 IP address type to DHCP (or enter a static IP on your LAN).",
            f"Set Address type to IP and SMB Server IP to {address or 'your server IP'}.",
            "Leave the port at 445.",
            f"Set Share name to exactly {share_name}.",
            (
                f"Set User to {username} and enter your password."
                if access_mode == "user"
                else "Set User to guest and leave the password blank."
            ),
            "Choose Save Changes, then Reconnect. The game list fills from the DVD and CD folders.",
        ],
    }
