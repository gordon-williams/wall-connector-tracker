# Wall Connector Tracker

Session logger and web dashboard for the **Tesla Gen 3 Wall Connector**. Polls the charger's local HTTP API every 30 seconds, records charging sessions to SQLite, and serves a responsive dark-mode web dashboard with cost tracking, trend charts, and multi-vehicle support.

Runs on a **Raspberry Pi** (systemd) or any always-on machine (macOS launchd). Tested on Python 3.9+.

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

### Multi-vehicle support
Any number of vehicles, each with a configurable name, maximum charge power, battery capacity, and flag for the off-peak EV rate. The server picks the closest match by power level.

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

## How session merging works

The Tesla Wall Connector's `session_energy_wh` counter is cumulative and does **not** reset during scheduled charging pauses (e.g. off-peak delay). When the poller sees charging resume within 2 hours and the counter reading is ≥ 90% of the previous session total, it re-opens the previous session record rather than creating a new one. The result is a single session row covering the entire plug event, with a continuous power-vs-time trend.

## How vehicle auto-detection works

After 2 minutes of charge data the server computes an average power and picks the first vehicle in `vehicles[]` whose `max_power_w` is ≥ that average. You can override any session's vehicle via the dashboard dropdown or the CLI `tag` command; the correct rate is applied automatically.

## Notes

- The Wall Connector local API is **unauthenticated** — accessible on your LAN without credentials
- `config.json` and `wc_sessions.db` are gitignored; they contain personal data
- The charger only exposes the *current* session; all historical data is accumulated by this server's continuous polling — don't stop the server between charges
- The Wall Connector API (`/api/1/vitals`) is undocumented and may change in future firmware

## License

MIT — see [LICENSE](LICENSE).
