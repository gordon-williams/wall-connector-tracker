# Wall Connector Tracker

Session logger and web dashboard for the **Tesla Gen 3 Wall Connector**. Polls the charger's local HTTP API every 30 seconds, records charging sessions to SQLite, and serves a dark-mode web dashboard with cost tracking.

![Dashboard screenshot — dark theme showing status card, session table, and vehicle summary](https://github.com/user-attachments/assets/placeholder)

## Features

- **Auto-detects sessions** — start, duration, and energy via the local Wall Connector API
- **Auto-tags vehicle** by average charge power (configurable threshold)
- **Two-rate support** — peak and off-peak rates (e.g. EV plan discounts applied automatically by session start time)
- **Web dashboard** — live status, filterable session table, inline vehicle/note editing, all-time summary
- **REST API** — `/api/status`, `/api/sessions`, `/api/config`, `/api/lifetime`
- **CLI client** — terminal access from any machine on the network
- **Runs as a macOS service** via launchd (always-on, survives reboots)

## Requirements

- Python 3.9+
- Flask

```
pip install flask
```

## Setup

**1. Find your Wall Connector's IP address**

Open your router's admin page and look for a device named `TeslaWallConnector` (or similar), or check via the Tesla app. Confirm the API works:

```
curl http://<your-ip>/api/1/vitals
```

**2. Configure**

```
cp config.example.json config.json
```

Edit `config.json`:

```json
{
    "wc_ip": "192.168.1.100",
    "rate_general_kwh": 0.30,
    "rate_ev_powerup_kwh": 0.08,
    "offpeak_start_hour": 22,
    "offpeak_end_hour": 7,
    "vehicle_power_threshold_w": 8750,
    "vehicles": ["Tesla", "Other"]
}
```

| Key | Description |
|---|---|
| `wc_ip` | Wall Connector local IP address |
| `rate_general_kwh` | Standard electricity rate ($/kWh) |
| `rate_ev_powerup_kwh` | Off-peak EV plan rate ($/kWh) — applied to high-power vehicle during off-peak hours |
| `offpeak_start_hour` / `offpeak_end_hour` | Off-peak window (24h local time) |
| `vehicle_power_threshold_w` | Power (W) above which the high-power vehicle is assumed (e.g. 8750 W for a 3-phase 11 kW charger vs single-phase 6.5 kW) |
| `vehicles` | `[high-power name, low-power name]` — used for auto-tagging |

**3. Start the server**

```
python3 wc_server.py
```

Dashboard: [http://localhost:8090/](http://localhost:8090/)

**4. (Optional) Run permanently on macOS**

```
cp launchd.plist.example ~/Library/LaunchAgents/com.yourname.wallconnector.plist
```

Edit the plist: replace `/path/to/python3` (find with `which python3`) and `/path/to/WallConnector/` with your actual paths.

```
launchctl load ~/Library/LaunchAgents/com.yourname.wallconnector.plist
```

## CLI client

Access the server from any machine on your network:

```bash
# From the same machine
python3 wc_client.py status
python3 wc_client.py report --days 30
python3 wc_client.py tag 42 tesla
python3 wc_client.py note 42 "Long trip home"

# From another machine
WC_SERVER=http://192.168.1.10:8090 python3 wc_client.py status
```

## API

| Endpoint | Description |
|---|---|
| `GET /api/status` | Live vitals + current session |
| `GET /api/sessions` | Session list (`?days=7`, `?month=2026-05`, `?vehicle=Tesla`) |
| `PATCH /api/sessions/<id>` | Update vehicle, notes, or rate |
| `GET /api/lifetime` | Lifetime counters from charger |
| `GET /api/config` | Current server configuration |
| `GET /api/summary` | All-time totals by vehicle |

## Vehicle auto-detection

The server calculates average charge power after 2 minutes of a session. Sessions above `vehicle_power_threshold_w` are tagged as the first vehicle in `vehicles[]`; sessions below are tagged as the second. You can override any session's vehicle tag via the dashboard dropdown or CLI.

Off-peak rate is applied automatically to the high-power vehicle when the session starts during the configured off-peak window.

## Notes

- The Wall Connector local API is unauthenticated — only accessible on your LAN
- `config.json` and `wc_sessions.db` are gitignored (personal data)
- The charger only exposes the *current* session; historical session data is accumulated by this server's continuous polling
