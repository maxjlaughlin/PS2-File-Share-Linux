# PS2 File Share

Share a folder of PS2 games from a Linux server so **Open PS2 Loader** can see
it, and watch what the console is doing while it plays.

Getting OPL onto a network share is normally a half-hour of hand-editing
`smb.conf`, rediscovering that modern Samba disabled SMB1 and NTLMv1 years ago,
creating eight folders with exactly the right names, and then guessing why the
game list is still empty. This does all of that from one page, and then shows
you live throughput, which console is connected, and which ISO it is reading.

```
sudo ./install.sh          # then open http://<your-server>:8723/
```

No pip, no virtualenv, no Node, no containers — it is pure Python standard
library and runs on any distribution with Python 3.8+ and Samba.

---

## What it does

**Sets the share up properly.** Pick a folder in the browser, press one button.
It writes a self-contained Samba config that re-enables the SMB1 dialect and
NTLMv1 authentication OPL needs, turns off oplocks (OPL cannot answer an oplock
break, which is a classic mid-game freeze), disables the per-file DOS-attribute
round-trips that make browsing a large library crawl, and switches on sendfile
and async I/O. It validates the result with `testparm` before reloading Samba,
and rolls back if Samba does not like it.

**Leaves your server alone.** Your `smb.conf` is backed up and gets exactly one
marked `include` line appended; everything else lives in its own file. Other
shares keep working, and Windows, macOS and phones still connect over SMB3
unless you explicitly ask for SMB1-only. `uninstall.sh` puts it all back.

**Creates the OPL folder layout** — `CD DVD VMC CFG ART THM LNG APPS` — and can
chown the whole share to the account Samba serves it as, which is the fix for
"OPL sees the share but the list is empty" and "my virtual memory card will not
save".

**Tells you what to type into the console.** The dashboard prints the server IP,
port, share name and user as OPL's Network Settings screen expects them, with a
copy button on each and the steps in order.

**Monitors properly.** Not a guess from the interface counters:

- Per-console throughput read from the kernel's own TCP byte counters, so two
  consoles on the same server are measured separately.
- The file each console has open right now — "Gran Turismo 4.iso, 4.00 GB" —
  read from the serving `smbd` process's own file descriptors, so it works on
  every Samba version without parsing `smbstatus`. A read position is shown
  too when the kernel reports one; with `sendfile` on it usually does not,
  because Samba reads at explicit offsets that never move the descriptor.
- Daily usage per console, kept in SQLite, surviving restarts.

**Checks your library.** Reads the game ID straight out of each ISO (and CSO)
by parsing ISO9660 down to `SYSTEM.CNF`, groups split `.iso.00` parts back into
one entry, and flags names longer than the 32 characters OPL will display or
sitting on a FAT32 volume that cannot hold a DVD image.

## Screens

| | |
|---|---|
| **Dashboard** | live throughput chart, connected consoles, what they are reading, the settings to type into OPL |
| **Share** | folder picker, share name, guest or password access, OPL folder and permission repair |
| **Games** | every image OPL will see, with game IDs, sizes and warnings |
| **Usage** | today / 7 / 30 day totals, a 14-day chart, per-console breakdown |
| **Advanced** | start/stop/reload Samba, compatibility switches, firewall, the generated config, logs |

## Install

```bash
git clone https://github.com/maxjlaughlin/PS2-File-Share-Linux.git
cd PS2-File-Share-Linux
sudo ./install.sh
```

The installer checks your Python, offers to install Samba if it is missing,
copies the app to `/opt/ps2share`, installs a systemd service and starts it.

**Set a password on the web interface before you walk away** — it runs as root,
because managing Samba and file ownership needs root:

```bash
sudo ps2share set-ui-password
```

### Without the installer

```bash
sudo python3 -m ps2share serve          # from the cloned directory
```

### As a pip package

```bash
pipx install .     # or: pip install .
sudo ps2share serve
```

## Using it from the command line

The whole thing works headless if you would rather not run a web interface:

```bash
sudo ps2share apply /srv/ps2games            # guest access, share name PS2SMB
sudo ps2share apply /srv/ps2games --user ps2 # ask for a username and password
sudo ps2share apply /mnt/games --name GAMES --read-only

ps2share status                              # is it up, what is shared, is 445 open
ps2share games                               # list what OPL will see, with game IDs
ps2share preview                             # print the Samba config without writing it
sudo ps2share remove                         # take the share back out of Samba
```

## Setting up the PS2

1. **Open PS2 Loader → Settings → Network Settings**
2. **PS2 IP address type**: DHCP (or a static address on your LAN)
3. **Address type**: IP
4. **SMB Server IP**: your server's address — the dashboard shows it
5. **Port**: 445
6. **Share name**: `PS2SMB` (or whatever you named it — it must match exactly)
7. **User**: `guest`, password blank — or the account you configured
8. **Save Changes**, then **Reconnect**

Put `.iso` files in the share's `DVD` folder (`CD` for CD-sized images) and they
appear under **Network Games**.

Expect roughly **2.5–4 MB/s on the console's 100 Mbit NIC** on a wired
connection. Over Wi-Fi via a bridge, expect less and expect stutter in FMV-heavy
games. Nothing in software fixes that; the PS2's network adapter is the ceiling.

## When it does not work

**OPL says it cannot connect.** Check the Dashboard pills: Samba running, port
445 open, no firewall banner. Then check the console and the server are on the
same subnet.

**It connects but the game list is empty.** The images must be directly inside
`DVD/` or `CD/`, not in sub-folders. The Games tab shows exactly what OPL will
see; if a file is missing from there, OPL will not see it either.

**It connects, then stalls or freezes partway into a game.** Almost always
oplocks — already off in the generated config — or a flaky link. Watch the
Dashboard while it happens: if throughput drops to zero but the connection
stays, it is the network, not Samba.

**A very old OPL build still refuses.** Advanced → Compatibility → *Allow LANMAN
authentication*, then re-apply. Try *Also listen on port 139* too.

**Virtual memory cards will not save.** The share must be writable (Share tab)
and the files must be owned by the account Samba serves as — press **Fix
permissions**.

## How it works

| Module | What it handles |
|---|---|
| `ps2share/samba.py` | generating, validating, applying and rolling back the Samba config |
| `ps2share/monitor.py` | socket byte counters, open-file inspection, the usage database |
| `ps2share/opl.py` | folder layout, ISO9660/CISO parsing, library checks |
| `ps2share/netinfo.py` | addresses, listening ports, firewall detection |
| `ps2share/webapp.py` | the HTTP server and JSON API |
| `ps2share/static/` | the interface — one HTML file, one CSS file, one JS file |

Config lives in `/etc/ps2share/config.json`, usage history in
`/var/lib/ps2share/usage.db`. Neither the SMB password nor the UI password is
stored in plain text: the first goes to Samba's own database, the second is
kept as a PBKDF2-SHA256 hash.

### Notes on the security trade-offs

- **SMB1 and NTLMv1 are old and weak.** There is no way around this: it is what
  the PS2 speaks. The generated config enables them for this server, so keep it
  on a home LAN and off the open internet. Leaving *SMB1 only* switched off
  means your other devices still negotiate SMB3.
- **The share never runs as root.** If the folder you pick is root-owned, the
  app serves it as `nobody` instead and tells you.
- **Symlinks may point outside the shared folder** by default, so you can keep
  games on other disks and link them in. Advanced → Compatibility turns that off.
- **The web interface runs as root.** Set a password on it. It only binds to
  port 8723 and is not exposed beyond your network unless you forward it.

## Development

```bash
python3 -m unittest discover -s tests -t .     # 90 tests, no dependencies
python3 -m ps2share serve --host 127.0.0.1     # run it from the source tree
```

The tests build a synthetic ISO9660 image in a temp directory, drive a real
HTTP server over loopback and exercise the Samba config generator, so they run
anywhere — no Samba and no PS2 required.

## License

MIT. See [LICENSE](LICENSE).
