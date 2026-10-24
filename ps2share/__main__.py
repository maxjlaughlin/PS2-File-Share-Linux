"""Command line entry point: `python3 -m ps2share` or the installed `ps2share`."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys

from . import __version__, netinfo, opl, samba
from .config import Config
from .util import human_bytes, is_root
from .webapp import serve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ps2share",
        description="Share a folder of PS2 games over SMB so Open PS2 Loader can see it.",
    )
    parser.add_argument("--version", action="version", version=f"ps2share {__version__}")
    parser.add_argument("--config", help="path to config.json")
    parser.add_argument("-v", "--verbose", action="store_true", help="log debug output")

    sub = parser.add_subparsers(dest="command")

    run_cmd = sub.add_parser("serve", help="run the web interface (default)")
    run_cmd.add_argument("--host", help="address to listen on (default 0.0.0.0)")
    run_cmd.add_argument("--port", type=int, help="port to listen on (default 8723)")

    apply_cmd = sub.add_parser("apply", help="create the share from the command line")
    apply_cmd.add_argument("path", help="folder to share")
    apply_cmd.add_argument("--name", default="", help="share name OPL connects to (default PS2SMB)")
    apply_cmd.add_argument("--user", default="", help="require this account instead of guest access")
    apply_cmd.add_argument("--create-user", action="store_true", help="create the account if missing")
    apply_cmd.add_argument("--read-only", action="store_true", help="do not let the PS2 write")
    apply_cmd.add_argument("--smb1-only", action="store_true", help="refuse SMB2/3 from other clients")

    sub.add_parser("status", help="show the current share and server status")

    firewall_cmd = sub.add_parser(
        "open-firewall",
        help="allow the web interface and SMB through this server's firewall",
    )
    firewall_cmd.add_argument("--network", default="", help="source range to allow, e.g. 192.168.1.0/24")
    firewall_cmd.add_argument("--ui-only", action="store_true", help="only open the web interface port")
    firewall_cmd.add_argument("--smb-only", action="store_true", help="only open the SMB ports")
    firewall_cmd.add_argument("--dry-run", action="store_true", help="print the commands without running them")
    sub.add_parser("preview", help="print the Samba config that would be written")
    sub.add_parser("remove", help="remove the share from Samba")
    sub.add_parser("games", help="list the images OPL will see")

    password_cmd = sub.add_parser("set-ui-password", help="set (or clear) the web interface password")
    password_cmd.add_argument("--clear", action="store_true", help="remove the password")

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    config = Config.load(args.config)
    command = args.command or "serve"

    if command == "serve":
        if getattr(args, "host", None):
            config.ui_host = args.host
        if getattr(args, "port", None):
            config.ui_port = args.port
        config.normalise()
        serve(config)
        return 0

    if command == "apply":
        return _cmd_apply(config, args)
    if command == "status":
        return _cmd_status(config)
    if command == "preview":
        print(samba.render_config(config))
        return 0
    if command == "remove":
        outcome = samba.unapply()
        print(outcome.message)
        if outcome.ok:
            config.applied = False
            config.save()
        return 0 if outcome.ok else 1
    if command == "games":
        return _cmd_games(config)
    if command == "open-firewall":
        return _cmd_open_firewall(config, args)
    if command == "set-ui-password":
        return _cmd_ui_password(config, args)

    parser.print_help()
    return 1


def _cmd_apply(config: Config, args) -> int:
    if not is_root():
        print("Applying the share needs root. Try: sudo ps2share apply …", file=sys.stderr)
        return 1
    path = os.path.abspath(args.path)
    if not os.path.isdir(path):
        print(f"{path} is not a folder.", file=sys.stderr)
        return 1

    config.share_path = path
    if args.name:
        config.share_name = args.name
    config.writable = not args.read_only
    config.smb1_only = args.smb1_only

    if args.user:
        config.access_mode = "user"
        config.username = args.user
        if not samba.user_exists(args.user):
            if not args.create_user:
                print(
                    f"No Linux account called '{args.user}'. Re-run with --create-user, "
                    "or choose an existing account.",
                    file=sys.stderr,
                )
                return 1
            created = samba.create_system_user(args.user, home=path)
            if not created.ok:
                print(f"Could not create '{args.user}': {created.text}", file=sys.stderr)
                return 1
        password = getpass.getpass(f"SMB password for {args.user}: ")
        confirm = getpass.getpass("Repeat: ")
        if password != confirm:
            print("Passwords did not match.", file=sys.stderr)
            return 1
        result = samba.set_smb_password(args.user, password)
        if not result.ok:
            print(f"Could not set the SMB password: {result.text}", file=sys.stderr)
            return 1
        config.smb_password_set = True
    else:
        config.access_mode = "guest"

    config.normalise()

    force_user = samba.resolve_force_user(config)
    uid = gid = None
    if samba.user_exists(force_user):
        import pwd

        entry = pwd.getpwnam(force_user)
        uid, gid = entry.pw_uid, entry.pw_gid
    created = opl.ensure_layout(path, uid, gid)
    if created:
        print("Created OPL folders: " + ", ".join(created))

    outcome = samba.apply(config)
    print(outcome.message)
    if outcome.details:
        print(outcome.details)
    if outcome.written:
        config.applied = True
        config.save()
    if not outcome.ok:
        return 1

    _print_opl_settings(config)
    return 0


def _print_opl_settings(config: Config) -> None:
    settings = netinfo.opl_settings(config.share_name, config.access_mode, config.username)
    print("\nType these into OPL → Settings → Network Settings:")
    print(f"  SMB Server IP : {settings['server_ip'] or '<this server>'}")
    print(f"  Port          : {settings['server_port']}")
    print(f"  Share         : {settings['share_name']}")
    print(f"  User          : {settings['user']}")
    print(f"  Password      : {settings['password']}")


def _cmd_status(config: Config) -> int:
    status = samba.samba_status()
    print(f"ps2share {__version__}")
    print(f"  Samba installed : {'yes (' + status['version'] + ')' if status['installed'] else 'no'}")
    smbd = status["smbd"]
    print(f"  smbd            : {smbd['state']} (unit: {smbd['unit'] or 'not found'})")
    print(f"  Share folder    : {config.share_path or '(not set)'}")
    print(f"  Share name      : {config.share_name}")
    print(f"  Access          : {config.access_mode}")
    listening = netinfo.read_listening_ports()
    print(f"  Port 445 open   : {'yes' if 445 in listening else 'no'}")

    # Every address, not just the detected one: on a server with Docker, a VPN
    # or two NICs, the address with the default route is not always the one
    # your console or laptop can reach.
    addresses = netinfo.interfaces()
    print(f"  Web interface   : port {config.ui_port} "
          f"({'listening' if config.ui_port in listening else 'NOT listening'})")
    if addresses:
        print("  Try reaching it on:")
        for row in addresses:
            tag = "  <- detected as primary" if row["primary"] else ""
            name = f"{row['name']:<8} " if row["name"] else ""
            print(f"      http://{row['address']}:{config.ui_port}/   {name}{row['network']}{tag}")
    else:
        print("  Addresses       : none found")

    firewall = netinfo.firewall_state()
    if firewall["tool"] and firewall["active"]:
        print(f"  Firewall        : {firewall['tool']} is active")
        print(f"      It may be blocking port {config.ui_port} and SMB.")
        print("      Open both with: sudo ps2share open-firewall")
    elif firewall["tool"]:
        print(f"  Firewall        : {firewall['tool']} present but inactive")
    else:
        print("  Firewall        : none detected")

    if config.share_path:
        health = opl.share_health(
            config.share_path, serve_as=samba.resolve_force_user(config), writable=config.writable
        )
        for problem in health["problems"]:
            print(f"  ! {problem}")
        for note in health["notes"]:
            print(f"  - {note}")
        disk = health.get("disk") or {}
        if disk.get("total"):
            print(f"  Disk            : {human_bytes(disk['free'])} free of {human_bytes(disk['total'])}")
    return 0


def _cmd_open_firewall(config: Config, args) -> int:
    state = netinfo.firewall_state()
    if not state["tool"]:
        print("No managed firewall detected (no ufw, firewalld or nftables ruleset).")
        print("If you still cannot reach the server, something upstream is filtering —")
        print("a router, a cloud security group, or a firewall this tool does not manage.")
        return 0

    network = args.network
    if not network:
        primary = next((row for row in netinfo.interfaces() if row["primary"]), None)
        network = str(primary["network"]) if primary else ""

    commands = netinfo.firewall_commands(
        network,
        smb=not args.ui_only,
        ui_port=0 if args.smb_only else config.ui_port,
    )
    if not commands:
        print(f"Nothing to do for {state['tool']}.")
        return 0

    print(f"Firewall: {state['tool']} ({'active' if state['active'] else 'inactive'})")
    print(f"Allowing from: {network or 'any private network'}\n")
    for command in commands:
        print(f"  {command}")
    if args.dry_run:
        print("\nDry run — nothing was changed.")
        return 0

    if not is_root():
        print("\nChanging the firewall needs root. Re-run with sudo.", file=sys.stderr)
        return 1

    print()
    result = netinfo.open_firewall(
        network,
        smb=not args.ui_only,
        ui_port=0 if args.smb_only else config.ui_port,
    )
    for ran in result["ran"]:
        print(f"  {'ok ' if ran['ok'] else 'FAILED'}  {ran['command']}")
        if not ran["ok"] and ran["output"]:
            print(f"         {ran['output']}")
    print(f"\n{result['message']}")
    return 0 if result["ok"] else 1


def _cmd_games(config: Config) -> int:
    if not config.share_path:
        print("No share folder configured yet.", file=sys.stderr)
        return 1
    scanner = opl.GameScanner()
    games = scanner.scan(config.share_path)
    if not games:
        print("No disc images found in CD/ or DVD/.")
        return 0
    width = max(len(game.name) for game in games)
    total = 0
    for game in games:
        total += game.size
        flags = " ".join(filter(None, [game.game_id, game.kind.upper()]))
        print(f"  {game.folder:3}  {game.name:<{width}}  {human_bytes(game.size):>9}  {flags}")
        for warning in game.warnings:
            print(f"       ! {warning}")
    print(f"\n  {len(games)} images, {human_bytes(total)} total")
    return 0


def _cmd_ui_password(config: Config, args) -> int:
    if args.clear:
        config.set_ui_password("")
        config.save()
        print("Web interface password removed.")
        return 0
    password = getpass.getpass("New web interface password: ")
    confirm = getpass.getpass("Repeat: ")
    if password != confirm:
        print("Passwords did not match.", file=sys.stderr)
        return 1
    if len(password) < 6:
        print("Use at least 6 characters.", file=sys.stderr)
        return 1
    config.set_ui_password(password)
    config.save()
    print("Web interface password set.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
