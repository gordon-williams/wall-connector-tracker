# Wall Connector Tracker

Session logger and web dashboard for the **Tesla Gen 3 Wall Connector**. Polls the charger's local HTTP API every 30 seconds, records charging sessions to SQLite, and serves a responsive dark-mode web dashboard with cost tracking, trend charts, and multi-vehicle support.

Runs on a **Raspberry Pi** (systemd) or any always-on machine (macOS launchd). Tested on Python 3.9+.

The history can also be mirrored to a password-protected server on the internet, so you can read it from anywhere without opening a single port at home — see [Remote access](#remote-access-private-internet-mirror).

## Features

### Session tracking
- **Auto-detects sessions** — start time, duration, and energy from the local Wall Connector API
- **Auto-tags vehicle** by average charge power after 2 minutes of data
- **Auto-merges paused sessions** — the Wall Connector counter doesn't reset during scheduled charging pauses; the server detects resumptions within 2 hours and continues the same session record
- **Per-session rate** — peak / off-peak rates applied automatically by session start time; override per-session via the dashboard

### Dashboard
- **Live status card** — current power, voltage, session energy, and elapsed time; polls every 10 seconds
- **Session table** — filterable by vehicle, date range, or month; inline editing for vehicle tag, SOC start/end, notes, and rate
- **Summary cards** — all-time session count, total energy, and cost per vehicle
- **Charging history chart** — bar chart by day / month / all time; toggle by vehicle
- **Trend chart** — per-session power profile (Power vs Energy *and* Power vs Time); gap periods shown dropping to zero; whole-hour x-axis labels
- **Dark / light theme toggle**
- **CSV export** — download the filtered session table as a spreadsheet
- **Copy chart to clipboard** — saves trend chart or histogram as a high-resolution PNG (3× pixel ratio)

### Reading it away from home
Three options, in increasing order of machinery:

- **Private network (recommended)** — put the server's machine on a [Tailscale](https://tailscale.com) tailnet and reach the real dashboard from anywhere. Live data, full editing, nothing copied or exported, no ports opened.
- **Offline copy** — a single self-contained HTML file, whole history baked in, written to a synced folder whenever a session finishes. No server and no account, but a snapshot rather than the live thing.
- **Private mirror** — the same program in `cloud` mode on an internet host. The server pushes over HTTPS; the mirror serves the identical dashboard behind a password. Use when you need a URL you can share.

### Multi-vehicle support
Any number of vehicles, each with a configurable name, maximum charge power, battery capacity, and flag for the off-peak EV rate. The server picks the closest match by power level.

Vehicles can be added, edited, or removed live from the **Settings page** (`/config`) — no need to edit `config.json` by hand or restart the server.

### REST API
| Endpoint | Description |
|---|---|
| `GET /api/status` | Live vitals + current session |
| `GET /api/sessions` | Session list (`?days=7`, `?month=2026-05`, `?vehicle=Tesla`) |
| `GET /api/sessions/<id>` | Single session |
| `PATCH /api/sessions/<id>` | Update vehicle, notes, rate, or SOC |
| `GET /api/sessions/<id>/samples` | 30-second sample log with computed power |
| `GET /api/summary` | All-time totals grouped by vehicle |
| `GET /api/lifetime` | Lifetime counters from the charger |
| `GET /api/config` | Current server configuration |
| `POST /api/sync` | *Mirror only* — authenticated ingest from the home server |
| `GET /healthz` | *Mirror only* — liveness and last sync time |

### CLI client
```bash
python3 wc_client.py status
python3 wc_client.py report --days 30
python3 wc_client.py tag 42 Tesla
python3 wc_client.py note 42 "Long trip home"

# Point at a remote server
WC_SERVER=http://192.168.1.10:8090 python3 wc_client.py status
```

## Requirements

- Python 3.9+
- Flask

```bash
pip install -r requirements.txt
```

## Recommended hardware

The server must run continuously — if it stops between charges, those sessions are lost. A dedicated low-power machine left on 24/7 is the right choice.

| Hardware | Notes |
|---|---|
| **Raspberry Pi 4 / 5** | Best option. Plenty of headroom, active community, ~3–5 W idle. |
| **Raspberry Pi Zero 2 W** | Cheapest option (~$15). Handles the polling load easily; slower to set up. |
| **Old laptop / NUC / mini PC** | Works fine if it's already on 24/7. |
| **macOS machine (always-on)** | Use the launchd setup (section 4b below). Sleep must be disabled or the server will miss sessions. |

A Pi 4 or Zero 2 W on your home network is the recommended setup. The server uses negligible resources — SQLite writes every 30 seconds, one Flask thread, no GPU or heavy computation.

## Setup

### 1. Find your Wall Connector's IP address

Open your router's admin page and look for a device named `TeslaWallConnector` (or similar), or check the Tesla app. Confirm the local API is reachable:

```bash
curl http://<your-wc-ip>/api/1/vitals
```

### 2. Configure

```bash
cp config.example.json config.json
```

Edit `config.json`:

```json
{
    "wc_ip": "192.168.1.100",

    "rate_general_kwh": 0.30,
    "rate_ev_powerup_kwh": 0.08,

    "offpeak_start_hour": 21,
    "offpeak_end_hour":   7,

    "vehicles": [
        {"name": "My EV",     "max_power_w": 7400,  "capacity_kwh": 60.0,  "ev_powerup": true},
        {"name": "Family EV", "max_power_w": 13000, "capacity_kwh": 82.0,  "ev_powerup": true},
        {"name": "PHEV",      "max_power_w": 3700,  "capacity_kwh": 26.5,  "ev_powerup": false},
        {"name": "Unknown",   "max_power_w": 9999,  "capacity_kwh": 0,     "ev_powerup": false}
    ]
}
```

| Key | Description |
|---|---|
| `wc_ip` | Wall Connector IP on your LAN |
| `rate_general_kwh` | Standard electricity rate ($/kWh) |
| `rate_ev_powerup_kwh` | Off-peak EV plan rate ($/kWh) |
| `offpeak_start_hour` / `offpeak_end_hour` | Off-peak window in 24-hour local time |
| `vehicles[].name` | Display name for the vehicle |
| `vehicles[].max_power_w` | Upper power limit used to identify the vehicle (server picks the first entry whose limit ≥ observed average charge power) |
| `vehicles[].capacity_kwh` | Battery capacity — used to estimate charging efficiency when SOC start/end are recorded |
| `vehicles[].ev_powerup` | `true` to apply the off-peak rate to this vehicle |

List vehicles from **lowest to highest** `max_power_w`. Keep an `"Unknown"` catch-all last.

### 3. Start the server

```bash
python3 wc_server.py
```

Dashboard: [http://localhost:8090/](http://localhost:8090/)

### 4a. Run permanently on Raspberry Pi (systemd)

```ini
# /etc/systemd/system/wallconnector.service
[Unit]
Description=Wall Connector Tracker
After=network.target

[Service]
ExecStart=/home/pi/wallconnector/venv/bin/python3 /home/pi/wallconnector/wc_server.py
WorkingDirectory=/home/pi/wallconnector
Restart=always
User=pi

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now wallconnector
sudo journalctl -u wallconnector -f   # follow logs
```

### 4b. Run permanently on macOS (launchd)

```bash
cp launchd.plist.example ~/Library/LaunchAgents/com.yourname.wallconnector.plist
```

Edit the plist — replace `/path/to/python3` (find with `which python3`) and `/path/to/WallConnector/` with your actual paths — then:

```bash
launchctl load ~/Library/LaunchAgents/com.yourname.wallconnector.plist
```

## Remote access: private network (recommended)

The dashboard already runs on your LAN — the only thing missing away from home is a route to it. A mesh VPN gives you that without exporting data, opening a router port, or running anything extra.

[Tailscale](https://tailscale.com) is free for personal use, has official ARM packages, and works on a Raspberry Pi.

**Install it on the machine running the server — the Pi — not on your laptop.** The Pi is the machine that has to become reachable; your other devices only need to join the same tailnet. SSH to the Pi first, then:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

```bash
sudo tailscale up
```

That prints a URL — open it, sign in, and the Pi joins your tailnet. Confirm with `tailscale status`.

Then install the Tailscale **app** on your phone and, if you want it, your Mac, and sign in with the same account. The dashboard is at `http://<pi-hostname>:8090/` from any signed-in device, exactly as it is at home.

> On macOS the install script does **not** install a command-line tool — it detects the platform, picks `method appstore`, and opens the App Store. The App Store build keeps its binary inside `/Applications/Tailscale.app/`, so `sudo tailscale up` on a Mac returns `command not found`. That's expected: on the Mac you sign in through the app's menu-bar icon, and `tailscale up` is a Linux/Pi step only.

Nothing about the server changes: it already binds `0.0.0.0`, so it is reachable over the tailnet interface with no config, no code changes and no restart.

### Why this is the safest of the three

- **No inbound ports.** Tailscale connects outbound; your router configuration is untouched.
- **Only your devices can reach it.** The dashboard has no password, which is fine here — the tailnet *is* the authentication. Nothing on the public internet can route to it at all.
- **No copies of your data.** The offline file and the mirror both duplicate your history somewhere else. This doesn't — there's exactly one database and you're reading it directly.
- **Everything works.** Live status, editing vehicle tags and notes, the Settings page. The other two options are read-only by design.

> ⚠️ Do **not** enable Tailscale **Funnel** on this machine. Funnel deliberately exposes a service to the public internet, which would put an unauthenticated dashboard online. Plain `tailscale up` does not do this.

If you'd rather not install anything on your phone, use the offline copy below instead.

## Backups

The tracker's database is the only record of your charging history — the charger itself exposes only the *current* session, so anything lost is lost for good. If the server runs on a Raspberry Pi, that history lives on an SD card, which is the component most likely to fail.

The recommended backup needs **no Python and no scripts to maintain** — two stock tools and one cron line on the machine running the server.

### Why not just upload the database file

In WAL mode recent commits live in the `-wal` sidecar, so copying `wc_sessions.db` on its own can capture a mid-write state or miss data. `sqlite3`'s `.backup` takes a consistent snapshot using SQLite's backup API, safely, even while a car is charging.

### Setup

```bash
sudo apt install -y sqlite3 rclone
```

Configure a Dropbox remote with `rclone config`. On a headless Pi the neatest route needs nothing installed on your laptop — open a tunnelled session first:

```bash
ssh -L 53682:localhost:53682 pi@<pi-address>
```

Then run `rclone config` inside it, name the remote `dropbox`, leave client id and secret blank, and answer **yes** to "Use auto config". It prints a `http://127.0.0.1:53682/auth?...` link; open that in your laptop's browser and the tunnel carries it to the Pi. Without the tunnel you would answer "no" and need rclone on a second machine to run [`rclone authorize`](https://rclone.org/remote_setup/).

Copy [pi-backup.sh](pi-backup.sh) to the server, make it executable, and add one cron line:

```
MAILTO=""
17 3 * * * /home/pi/wallconnector/backup.sh
```

Edit the paths at the top of the script to match your install.

### Why a script and not a one-line cron entry

The obvious one-liner is `sqlite3 … ".backup /tmp/x.db" && rclone copy /tmp/x.db dropbox:…`, and it is **dangerous**. `sqlite3` creates a new, empty database when handed a path that doesn't exist — so if the database is ever moved, renamed, or lost, the snapshot "succeeds", produces a valid but empty 4 KB file, and rclone faithfully uploads it over your good backup. Every command returns 0, so nothing is logged and nothing looks wrong until you need the backup.

The script exists to check the snapshot before it is allowed to replace anything:

- the source database must exist before we start
- `integrity_check` must return `ok`
- the snapshot must contain at least `MIN_SESSIONS` sessions — sessions only accumulate, so a shrinking backup means something is wrong at the source

Any of those failing writes one line to `backup.log` and exits without uploading, leaving the previous backup intact. An empty log means every run has succeeded.

### Restoring

Verified end to end on a live Pi: the Dropbox copy was pulled down, checked, and run as a real server, serving every session correctly.

```bash
# 1. Fetch the backup
rclone copy dropbox:Charging/wc-history.db /tmp/

# 2. Verify it BEFORE trusting it — never restore a file you haven't checked
sqlite3 /tmp/wc-history.db "pragma integrity_check; select count(*) from sessions;"

# 3. Stop the server so nothing is writing
sudo systemctl stop wallconnector

# 4. Keep the current database rather than deleting it
mv /home/pi/wallconnector/wc_sessions.db /home/pi/wallconnector/wc_sessions.db.old

# 5. Remove the WAL sidecars — they belong to the OLD database
rm -f /home/pi/wallconnector/wc_sessions.db-wal /home/pi/wallconnector/wc_sessions.db-shm

# 6. Put the backup in place, owned by the service user
cp /tmp/wc-history.db /home/pi/wallconnector/wc_sessions.db
chown pi:pi /home/pi/wallconnector/wc_sessions.db

# 7. Start it
sudo systemctl start wallconnector

# 8. Confirm
curl -s localhost:8090/api/summary
```

Step 5 matters. `-wal` and `-shm` describe the database they were created beside; leaving them next to a different file invites SQLite to reconcile two things that never belonged together. The backup is a complete database in its own right and needs no sidecar.

Step 4 matters too — whatever is wrong with the current database, it holds every session since the last backup. Keep it until the restore is proven.

### Rehearsing a restore without disturbing anything

Because the server derives its database path from its own location, a full rehearsal is just a scratch directory and a spare port:

```bash
mkdir /tmp/restore-test && cd /tmp/restore-test
rclone copy dropbox:Charging/wc-history.db .
mv wc-history.db wc_sessions.db
cp /home/pi/wallconnector/wc_server.py /home/pi/wallconnector/config.json .
/home/pi/wallconnector/venv/bin/python wc_server.py --port 8099
```

Open `http://<host>:8099/` and check the history is all there, then stop it and delete the directory. Production is never touched. Worth doing once a year — an untested backup is a guess.

> When killing the rehearsal, match the process precisely (`pkill -f 'port 8099'` will also match the shell you typed it in). Stopping it with Ctrl-C is safer.

### What the backup does not cover

Only `wc_sessions.db`. If the whole machine is lost you will also need to reinstate Python and Flask, `wc_server.py`, the systemd unit, the rclone config — and `config.json`, which holds your rates, off-peak window and vehicle definitions. The database is the irreplaceable part; the rest is reconstructible but tedious. Adding `config.json` to the same upload is a sensible extension.

### Backing up into a synced folder

Writing backups into Dropbox does **not** contradict the warning below about keeping the live database out of one. The hazard there is a database being actively written by a running server while a sync client copies it mid-write. A backup is written once, closed, and uploaded as a complete file — no `-wal` beside it, no process holding it open. Syncing it gives you an offsite copy and version history for free.

### Ad-hoc backups over the network

`wc_backup.py` pulls a running tracker's full history over its REST API into a local SQLite file, for when you want a copy on a different machine without touching the server:

```bash
python3 wc_backup.py http://192.168.86.64:8090 --out ~/Desktop/wc-history.db
```

It needs no access to the server beyond HTTP, verifies the result before replacing any previous copy, and leaves the old one untouched if the source is unreachable. Useful as a manual tool; the cron entry above is what you should rely on.

## Where the database lives

By default `wc_sessions.db` sits next to `wc_server.py`. Point it somewhere else with `db_path` in `config.json`, or `--db`:

```json
"db_path": "~/Library/Application Support/WallConnector/wc_sessions.db"
```

**Keep it out of Dropbox, iCloud Drive, OneDrive and Google Drive.** A file-syncing service can upload the database and its `-wal` sidecar at different moments, or copy one mid-write; on restore the two disagree and committed transactions are silently lost. This is a [documented way to corrupt SQLite](https://www.sqlite.org/howtocorrupt.html), and the risk climbs sharply if two copies of the server ever run at once. The server warns at startup if it finds itself in one of these folders.

To move an existing database safely:

```bash
python3 wc_server.py --migrate-db ~/Library/Application\ Support/WallConnector/wc_sessions.db
```

That takes a consistent snapshot using SQLite's own backup API (correct even mid-write and in WAL mode), verifies the copy with `integrity_check` and a row-count comparison, and only then points `config.json` at the new location. It refuses to overwrite an existing file and never deletes the original — restart the server, confirm it works, then remove the old one yourself.

## Offline copy in a synced folder

The simplest way to read the history from a phone. The home server writes one **self-contained HTML file** — the same dashboard, with every session, sample and total baked in — into a folder that Dropbox or iCloud already syncs. You open the file on your phone. Nothing is hosted, nothing is exposed, and there is no password to manage beyond the one on your storage account.

```json
"offline_export": {
    "enabled": true,
    "path":    "~/Dropbox/Charging/charge-history.html"
}
```

It's regenerated at startup, whenever a charging session ends, and whenever you edit a session's vehicle, notes, SOC or rate. Writing goes to a temporary name and is renamed into place, so the sync client never uploads a half-written page. For the current history — 63 sessions and ~12,900 samples — the file is about 1.6 MB and takes ~85 ms to build.

Generate one by hand at any time:

```bash
python3 wc_server.py --export-offline ~/Desktop/charge-history.html
```

### How it works

The dashboard is a static page that talks to `/api/*`. Rather than maintain a second copy of it, the export inlines every API response the page asks for and installs a `fetch` that answers from that data. One template serves the live server, the mirror and the offline file — so any change to the dashboard reaches all three.

Everything works except the things that need a charger or a database: the session table with all its filters, both charts, per-session trend charts, summaries, the theme toggle and CSV export. There's no live status card (there's no charger behind a file) and editing is disabled, exactly as on the mirror.

### Things to know

- **The charts load Chart.js from a CDN**, so viewing needs an internet connection — just not a server of your own.
- **Check it renders on your phone before relying on it.** iOS previews HTML through WebKit and should run it fine, but tap the file once and confirm rather than assume.
- **Dropbox cannot host this as a website.** Dropbox [discontinued HTML rendering](https://help.dropbox.com/share/public-folder) in 2016–17, so a shared link offers the file for download rather than rendering it. This is a file you open, not a URL you visit. A shared link would also have no password — anyone holding the link would have the data.

## Remote access: private internet mirror

The Wall Connector's API is LAN-only, so the poller has to stay at home. To read the history from anywhere, the home server **pushes** it to a second copy of this same program running in `cloud` mode on an internet host:

```
Wall Connector ──LAN──▶  home server  ──HTTPS push──▶   mirror    ──login──▶  your phone
(unauthenticated)       polls, edits     outbound     (read-only)
```

Nothing inbound is opened on your home network — the Pi makes an outgoing HTTPS request like any other program on your LAN. The mirror only ever holds a copy: it has no route to the charger and it rejects every write.

| | Home server | Mirror |
|---|---|---|
| Session history, charts, summaries, CSV export | ✅ | ✅ |
| Live status card | ✅ live | last synced snapshot |
| Edit vehicle / notes / SOC / rate | ✅ | ❌ `403` |
| Settings page (rates, vehicles) | ✅ | ❌ redirects to the dashboard |
| Needs the Wall Connector on its LAN | ✅ | ❌ |

Edits stay on the home server and flow outward on the next sync.

### Where to host the mirror

It's a long-running Python process with a SQLite file on disk, so the host has to give you both:

| Host | Notes |
|---|---|
| **Small VPS** — Hetzner, DigitalOcean, Vultr, Linode | ~$4–6/month. The systemd unit in section 4a works unchanged. |
| **Fly.io / Render / Railway** | Container hosts with a persistent volume — mount it at the database path. |
| **Oracle Cloud free tier** | Free always-on VM, if you can get one. |

**Netlify, Vercel and Cloudflare can't run *this* mirror as written — but the reason is the runtime, not storage.** [Netlify Database](https://www.netlify.com/platform/database/) (serverless Postgres, generally available April 2026) would cover persistence fine. The blocker is that Netlify Functions run JavaScript, TypeScript and Go only, so a Flask app can't be deployed there.

Worth knowing if you're weighing it up: the mirror does no background work at all — it's purely request/response — so it *would* fit the serverless model. Porting it means reimplementing the mirror in TypeScript against Postgres and maintaining a second copy of the dashboard alongside this one. The sync protocol itself is portable either way: the home server just POSTs gzipped JSON with a bearer token, which any function runtime can receive.

If your domain is managed at Netlify, the least-effort path is to keep DNS there and point a `charge.` subdomain at a host that runs Python.

### 1. Set up the mirror

Copy the project to the host, then:

```bash
cp config.cloud.example.json config.json
python3 wc_server.py --gen-token       # shared secret — copy it
python3 wc_server.py --hash-password   # prompts, prints a password hash
```

Put both into its `config.json`:

```json
{
    "mode": "cloud",
    "cloud": {
        "sync_token":    "<the generated token>",
        "password_hash": "pbkdf2_sha256$240000$…",
        "require_https": true
    }
}
```

Bind it to localhost and terminate TLS with a reverse proxy:

```bash
python3 wc_server.py --mode cloud --host 127.0.0.1 --port 8090
```

Caddy needs three lines and handles certificates itself:

```
charge.example.com {
    reverse_proxy 127.0.0.1:8090
}
```

To run it under a production WSGI server instead of Flask's built-in one:

```bash
pip install gunicorn
WC_MODE=cloud gunicorn -w 1 --threads 8 -b 127.0.0.1:8090 'wc_server:create_app()'
```

Use a **single worker** — the SQLite database isn't shared between processes. `create_app()` reads `WC_MODE`, `WC_CONFIG` and `WC_DB` from the environment.

> `require_https` marks the login cookie `Secure`. Leave it `true` in production; set it to `false` only when testing over plain HTTP, or the browser will drop the cookie and you won't be able to log in.

### 2. Point the home server at it

Add a `sync` block to the home `config.json`, using the **same token**:

```json
"sync": {
    "enabled":      true,
    "url":          "https://charge.example.com",
    "token":        "<the same token>",
    "interval_s":   300,
    "sample_batch": 5000
}
```

Restart the home server. The first pass backfills the whole history in batches; after that each cycle sends only what changed:

```
Sync → https://charge.example.com every 300s
[sync] 63 session(s), 5000 sample(s) → mirror
[sync] backfill 5000 sample(s)
[sync] backfill 2893 sample(s)
```

Open `https://charge.example.com`, log in, and the history is there.

### What gets synced

| Data | When |
|---|---|
| Session rows | whenever any row changes — a session finishes, or an old one is edited |
| 30-second samples | with their session, once it finishes |
| Rates and vehicles | with any push, so the mirror renders costs and colours identically |
| Live status snapshot | only when `completed_only` is `false` |

By default (`"completed_only": true`) the mirror is a **history archive**: nothing is sent while a car is charging, and the whole session — row plus every sample — goes out in one push when it ends. That's one network round-trip per session instead of one every polling cycle, which matters a great deal on a metered host. The trade is that the mirror has no live view; you read live status on the LAN dashboard, where the charger actually is.

Set `"completed_only": false` for a near-live mirror that also carries the status card.

Two other knobs keep idle time genuinely idle:

- **Nothing changed, nothing sent.** The sync thread compares a fingerprint of the sessions table and a sample watermark; if neither moved it skips the request entirely rather than sending an empty one. `idle_heartbeat_s` (default 24 h, `0` to disable) forces an occasional push anyway so the mirror can show it's still being fed.
- **The dashboard stops polling when its tab is hidden**, and refreshes on the way back. A forgotten background tab used to poll all night.

Samples are the bulk of the data and payloads are gzipped, so a typical session push is a few kilobytes. The mirror's header shows when it last heard from home — green under 15 minutes, amber under an hour, red beyond that.

### If the mirror loses its data

Rebuild the host and start it with an empty database. The next time the home server pushes, it compares the row count the mirror reports against what it sent, spots the shortfall, and re-pushes everything.

Note the timing: because idle cycles skip the network entirely, "the next time it pushes" means the next finished session, the next edit, or the `idle_heartbeat_s` heartbeat — whichever comes first, so within a day by default. Lower `idle_heartbeat_s` if you want a lost mirror noticed sooner, at the cost of waking its database more often. To repair it immediately:

```bash
python3 wc_server.py --resync
```

### Security notes

- The home server keeps **no inbound ports open** — sync is an outbound HTTPS POST
- The mirror is behind a password (PBKDF2-SHA256, 240k iterations); the cookie is `HttpOnly`, `SameSite=Lax` and `Secure`. Five bad guesses lock that IP out for a minute
- `/api/sync` authenticates with a bearer token compared in constant time, and is the only route that bypasses the login
- The mirror never learns your charger's LAN address — `wc_ip` is not in the payload and not served by the mirror's `/api/config`
- The token and password hash live in the mirror's `config.json`, which is gitignored

## How session merging works

The Tesla Wall Connector's `session_energy_wh` counter is cumulative and does **not** reset during scheduled charging pauses (e.g. off-peak delay). When the poller sees charging resume within 2 hours and the counter reading is ≥ 90% of the previous session total, it re-opens the previous session record rather than creating a new one. The result is a single session row covering the entire plug event, with a continuous power-vs-time trend.

## How vehicle auto-detection works

After 2 minutes of charge data the server computes an average power and picks the first vehicle in `vehicles[]` whose `max_power_w` is ≥ that average. You can override any session's vehicle via the dashboard dropdown or the CLI `tag` command; the correct rate is applied automatically.

> **Limitation:** if two vehicles charge at the same power level (e.g. two EVs on single-phase 7 kW), auto-detection cannot tell them apart and will always tag the lower-threshold entry. Workaround: limit one vehicle's charge rate in its app settings to create a detectable power gap, or correct the tag manually after each session.

## Notes

- The Wall Connector local API is **unauthenticated** — accessible on your LAN without credentials
- `config.json` and `wc_sessions.db` are gitignored; they contain personal data
- The charger only exposes the *current* session; all historical data is accumulated by this server's continuous polling — don't stop the server between charges
- The Wall Connector API (`/api/1/vitals`) is undocumented and may change in future firmware
- The mirror is a copy, not a backup of last resort — it holds no data the home server doesn't, and it can't repopulate the home server
- Running two copies of the server against the same charger is harmless to the charger (its API is read-only) but produces two databases that silently diverge — each only records sessions that happened while it was running
- The offline HTML file is a snapshot, not a backup — it holds no data the database doesn't, and can't be loaded back in
- **SD card wear (Raspberry Pi):** the server only writes to SQLite while a charging session is active (~2 writes per 30 s). Idle periods produce no writes at all, so total write volume is low. The database is opened in WAL mode (`PRAGMA journal_mode=WAL`) for efficient sequential writes; a standard SD card will last for years under this workload

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgements

- [Flask](https://flask.palletsprojects.com/) — BSD 3-Clause (Pallets)
- [Chart.js](https://www.chartjs.org/) — MIT (Chart.js Contributors)
- [html2canvas](https://html2canvas.hertzen.com/) — MIT (Niklas von Hertzen)

Full license texts in [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES).
