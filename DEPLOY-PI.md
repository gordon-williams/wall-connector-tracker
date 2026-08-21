# Deploying to the Raspberry Pi

Upgrading the tracker on `raspberrypi4` (`192.168.86.64`) from the legacy build to the current one.

Written to be run by you, from the Mac and over SSH. Some steps are **discovery** rather than instructions: the Pi's install layout was never inspected, so it has to be confirmed on the day.

---

## Read this first

**The Pi holds the only complete copy of your charge history** — 91 sessions, 1,189.7 kWh, $321.94 as of 21 Aug 2026. There is no second copy anywhere. Step 1 exists for that reason; don't skip it.

**No sessions are recorded while the server is stopped.** If a car charges during the deploy, that session is lost permanently — the Wall Connector only exposes the *current* session, so there's nothing to backfill from. Do this when nothing is plugged in.

**Never copy `config.json` from the Mac to the Pi.** They differ: the Pi's off-peak window starts at 21:00, the Mac's at 22:00. Copying would silently re-rate sessions. Only `wc_server.py` gets deployed.

---

## What this upgrade changes

| Change | Effect on the Pi |
|---|---|
| Dashboard stops polling when its browser tab is hidden | Less load on the Pi; a forgotten tab no longer polls all night |
| `localStorage` calls wrapped in `try`/`catch` | Page survives browsers that block it |
| Nav links rendered from a build flag in JS | No visible difference |
| New `meta` table created at startup | Additive, empty unless sync is enabled |
| `save_config()` merges instead of overwriting | Editing vehicles in Settings no longer wipes other config keys |
| `db_path` config key and `--db` flag | Optional; leave unset and nothing moves |
| Warns if the database sits in Dropbox/iCloud/OneDrive | Won't trigger on the Pi |
| New flags: `--migrate-db`, `--export-offline`, `--mode`, `--gen-token`, `--hash-password`, `--resync` | All opt-in |
| Cloud mirror mode, push sync, offline HTML export | **All default to off** |

**With your existing `config.json` untouched, behaviour is identical** apart from the dashboard improvements and the new empty table. Nothing new turns itself on.

Requirements are unchanged: Python 3.9+, Flask, standard library only. No new dependencies.

---

## Getting in

The SSH user is **`pi`** (`ssh pi@192.168.86.64`). The Pi accepts both `publickey` and `password`.

Your keys are on the **Mac Studio**, not the MacBook — so the simplest route is to run this from the Mac Studio, where `ssh pi@192.168.86.64` already works without a prompt.

To work from the MacBook instead, install its key on the Pi once. This prompts for the Pi's password, which you type yourself:

```bash
ssh-copy-id -i ~/.ssh/id_ed25519_codex_macbook.pub pi@192.168.86.64
```

After that, `scp` and `ssh` from the MacBook work without a prompt for the rest of the deploy.

> There is no "default" password to fall back on. Raspberry Pi OS dropped the old `pi`/`raspberry` default in the Bullseye release (April 2022); current images make you set one at first boot. If it's genuinely lost, the recovery route is editing the SD card on another machine — a bigger detour than it's worth mid-deploy, so confirm you can log in *before* stopping anything.

---

## Step 1 — Snapshot and back up

### 1a. Record the Pi's current state (from the Mac)

```bash
python3 wc_healthcheck.py http://192.168.86.64:8090 --save ~/Desktop/pi-before.json
```

Expect `legacy build`, 91 sessions, `polling: ok`. This file is what proves afterwards that nothing was lost.

### 1b. Back up the database on the Pi

SSH in, then — from the directory holding `wc_sessions.db`:

```bash
python3 -c "
import sqlite3, time
src = sqlite3.connect('file:wc_sessions.db?mode=ro', uri=True)
dst = sqlite3.connect('wc_sessions.backup-%s.db' % time.strftime('%Y%m%d-%H%M%S'))
with dst: src.backup(dst)
print(dst.execute('select count(*) from sessions').fetchone()[0], 'sessions')
print('integrity:', dst.execute('pragma integrity_check').fetchone()[0])
"
```

This uses SQLite's backup API, so it's a consistent snapshot even with the server running. It should print **91 sessions** and **integrity: ok**.

### 1c. Copy that backup off the Pi

A backup on the same SD card doesn't protect against the SD card. From the Mac:

```bash
scp pi@192.168.86.64:<path>/wc_sessions.backup-*.db ~/Desktop/
```

**Do not continue until you have a verified copy on the Mac.**

---

## Step 2 — Find the install

These weren't verifiable remotely. On the Pi:

```bash
systemctl list-units --type=service | grep -i wall
systemctl show wallconnector -p ExecStart -p WorkingDirectory -p User
```

If there's no systemd unit, find it the other way:

```bash
ps aux | grep [w]c_server.py
sudo find / -name wc_server.py -not -path '*/proc/*' 2>/dev/null
```

Note down three things:

- **service name** (the README's example uses `wallconnector`)
- **working directory** — where `wc_server.py` and `wc_sessions.db` live
- **python interpreter** — a virtualenv path, or plain `python3`

Confirm the interpreter has Flask:

```bash
<python-path> -c "import flask, sys; print(sys.version.split()[0], 'flask', flask.__version__)"
```

Python **3.9 or newer** and any Flask 2.x or 3.x is fine. The new code uses no syntax newer than 3.9 and no dependencies beyond Flask.

---

## Step 3 — Deploy the code

### 3a. Keep the old version

On the Pi, in the working directory:

```bash
cp wc_server.py wc_server.py.legacy
```

That file is your rollback. Leave it there until you're satisfied.

### 3b. Copy the new version across

From the Mac, in the project directory:

```bash
scp wc_server.py wc_healthcheck.py pi@192.168.86.64:<working-directory>/
```

If the Pi is a git clone of this repo, `git pull` there instead — but check `git status` first, in case the Pi has local edits that never made it back.

### 3c. Syntax-check before restarting

```bash
<python-path> -c "import ast; ast.parse(open('wc_server.py').read()); print('parses ok')"
```

---

## Step 4 — Restart and verify

```bash
sudo systemctl restart wallconnector
```

```bash
sudo journalctl -u wallconnector -n 30 --no-pager
```

Expect to see `Poller started — 30s interval → 192.168.86.47` and no traceback. You should **not** see any warning about a cloud-synced folder.

Then, from the Mac:

```bash
python3 wc_healthcheck.py http://192.168.86.64:8090 --compare ~/Desktop/pi-before.json
```

A good result reports `build changed: legacy → templated` as a note, and:

```
  ✓ no data lost, config unchanged, server healthy
```

It exits non-zero and prints `PROBLEM` lines if any session disappeared, the totals dropped, the rates changed, or the poller isn't running. **Treat any `PROBLEM` line as a failed deploy** and roll back.

Finally, open `http://192.168.86.64:8090/` and confirm the session table, both charts and a per-session trend chart all render.

---

## Step 5 — Rollback

If anything looks wrong:

```bash
cd <working-directory> && cp wc_server.py.legacy wc_server.py && sudo systemctl restart wallconnector
```

The database is untouched by the upgrade — the only schema change is an added empty `meta` table, which the legacy code ignores. Rolling back the code is sufficient; you should not need the database backup. It exists in case something unexpected happens.

---

## After it's stable

Optional, in the order I'd do them:

1. **Tailscale** — remote access to the live dashboard. Install it **on the Pi**, over SSH: `curl -fsSL https://tailscale.com/install.sh | sh` then `sudo tailscale up`. Needs no changes to the server, which already binds `0.0.0.0`.
   - Running the same script on a Mac installs the App Store app instead and leaves no `tailscale` command — `sudo tailscale up` there returns `command not found`. On the Mac and phone you just install the app and sign in.
   - Never enable Tailscale **Funnel**: the dashboard has no password because the tailnet is the authentication.

2. **Delete the backups** once you've had a couple of successful charging sessions — `wc_server.py.legacy` on the Pi and the `.db` copies on the Desktop.

3. **Offline HTML export** — only if you want a file-based copy as well. Note that Dropbox has no ARM Linux client, so the Pi would need `rclone` to deliver it.

4. **Cloud mirror** (`--mode cloud`) — only if you later want a URL you can share with someone who isn't on your tailnet.

---

## Checklist

- [ ] SSH access to `pi@192.168.86.64` confirmed working
- [ ] Nothing charging
- [ ] `pi-before.json` saved on the Mac
- [ ] Database backup taken on the Pi, integrity `ok`, 91 sessions
- [ ] Backup copied to the Mac
- [ ] Service name, working directory and python path noted
- [ ] `wc_server.py.legacy` created on the Pi
- [ ] New `wc_server.py` copied and parses
- [ ] Service restarted, journal clean
- [ ] `--compare` reports no data lost
- [ ] Dashboard renders and charts draw
