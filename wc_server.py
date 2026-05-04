#!/usr/bin/env python3
"""Wall Connector Server — polls charger, stores sessions, serves web dashboard + REST API.

Usage:
    python3 wc_server.py              # start on port 8090
    python3 wc_server.py --port 8090

Dashboard: http://localhost:8090/
API:        http://localhost:8090/api/status
            http://localhost:8090/api/sessions
            http://localhost:8090/api/lifetime
"""

import argparse
import base64
import json
import os
import signal
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from threading import Lock, Thread

from flask import Flask, Response, jsonify, request

# ── Config ────────────────────────────────────────────────────────────────────

# Fixed at startup — changing these requires a restart
WC_IP       = ""
POLL_S      = 30
DB_PATH     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wc_sessions.db")
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

AUTO_TAG_AFTER_S = 120   # seconds of data before auto-tagging vehicle

# Live-editable config — updated by PATCH /api/config, persisted to config.json
CONFIG: dict = {
    "rate_general_kwh":    0.30,
    "rate_ev_powerup_kwh": 0.08,
    "offpeak_start_hour":  22,
    "offpeak_end_hour":    7,
    "vehicles": [
        {"name": "Tesla",   "max_power_w": 13000, "ev_powerup": True},
        {"name": "Shark",   "max_power_w":  7000, "ev_powerup": False},
        {"name": "Unknown", "max_power_w":  9999, "ev_powerup": False},
    ],
}
CONFIG_LOCK = Lock()

app = Flask(__name__)

# ── Shared poller state (guarded by state_lock) ───────────────────────────────

state_lock   = Lock()
poller_state = {
    "session_id":     None,
    "was_charging":   False,
    "session_energy": 0.0,
    "session_start":  None,
    "auto_tagged":    False,
    "last_vitals":    None,
    "last_poll_ts":   None,
    "poll_error":     False,
}


# ── Database ──────────────────────────────────────────────────────────────────

def make_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = make_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            start_time  TEXT NOT NULL,
            end_time    TEXT,
            duration_s  INTEGER,
            energy_wh   REAL,
            vehicle     TEXT    DEFAULT 'Unknown',
            auto_tagged INTEGER DEFAULT 0,
            rate_kwh    REAL    DEFAULT 0.352440,
            notes       TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_samples (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  INTEGER NOT NULL,
            ts          TEXT NOT NULL,
            energy_wh   REAL,
            current_a   REAL,
            grid_v      REAL
        )
    """)
    conn.commit()
    conn.close()


_db_lock = Lock()


def db_exec(sql, params=()):
    with _db_lock:
        conn = make_conn()
        cur  = conn.execute(sql, params)
        conn.commit()
        result = cur.lastrowid
        conn.close()
        return result


def db_query(sql, params=()):
    conn = make_conn()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def db_one(sql, params=()):
    conn = make_conn()
    row  = conn.execute(sql, params).fetchone()
    conn.close()
    return dict(row) if row else None


# ── API helpers ───────────────────────────────────────────────────────────────

def fetch_json(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return None


def save_config():
    full = {"wc_ip": WC_IP, **CONFIG}
    with open(CONFIG_PATH, "w") as f:
        json.dump(full, f, indent=4)


def detect_vehicle(avg_power_w: float) -> str:
    """Match observed average power to the lowest-capacity vehicle that can explain it."""
    vs = sorted(CONFIG["vehicles"], key=lambda v: v.get("max_power_w", 0))
    for v in vs:
        if avg_power_w <= v.get("max_power_w", 0) * 1.2:
            return v["name"]
    return vs[-1]["name"] if vs else "Unknown"


def rate_for_vehicle(vehicle_name: str, start_iso: str) -> float:
    for v in CONFIG["vehicles"]:
        if v["name"] == vehicle_name and v.get("ev_powerup", False):
            try:
                dt = datetime.fromisoformat(start_iso).astimezone()
                h  = dt.hour
                s, e = CONFIG["offpeak_start_hour"], CONFIG["offpeak_end_hour"]
                if h >= s or h < e:
                    return CONFIG["rate_ev_powerup_kwh"]
            except Exception:
                pass
    return CONFIG["rate_general_kwh"]


def fmt_duration(s):
    if not s:
        return None
    h, rem = divmod(int(s), 3600)
    m = rem // 60
    return f"{h}h {m:02d}m" if h else f"{m}m"


def session_cost(row: dict) -> float:
    wh   = row.get("energy_wh") or 0
    rate = row.get("rate_kwh") or CONFIG["rate_general_kwh"]
    return round(wh / 1000 * rate, 4)


# ── Background poller ─────────────────────────────────────────────────────────

def poller():
    print(f"Poller started — {POLL_S}s interval → {WC_IP}")

    while True:
        v       = fetch_json(f"http://{WC_IP}/api/1/vitals")
        now_utc = datetime.now(timezone.utc)
        now_iso = now_utc.isoformat()

        with state_lock:
            if v is None:
                poller_state["poll_error"]   = True
                poller_state["last_poll_ts"] = now_iso
                time.sleep(POLL_S)
                continue

            poller_state["last_vitals"]  = v
            poller_state["last_poll_ts"] = now_iso
            poller_state["poll_error"]   = False

            is_charging   = bool(v.get("contactor_closed"))
            energy_wh     = float(v.get("session_energy_wh") or 0)
            session_s     = int(v.get("session_s") or 0)
            was_charging  = poller_state["was_charging"]
            session_id    = poller_state["session_id"]

            # ── New session ────────────────────────────────────────────────
            if is_charging and not was_charging:
                sid = db_exec(
                    "INSERT INTO sessions (start_time, rate_kwh, energy_wh, vehicle) VALUES (?, ?, ?, 'Unknown')",
                    (now_iso, RATE_GENERAL, energy_wh)
                )
                poller_state.update(session_id=sid, session_energy=energy_wh,
                                    session_start=now_utc, auto_tagged=False)
                print(f"[{now_utc.strftime('%H:%M:%S')}] Session {sid} started")

            # ── Daemon started mid-session ─────────────────────────────────
            elif is_charging and not session_id:
                sid = db_exec(
                    "INSERT INTO sessions (start_time, rate_kwh, energy_wh, vehicle) VALUES (?, ?, ?, 'Unknown')",
                    (now_iso, RATE_GENERAL, energy_wh)
                )
                poller_state.update(session_id=sid, session_energy=energy_wh,
                                    session_start=now_utc, auto_tagged=False)
                print(f"[{now_utc.strftime('%H:%M:%S')}] Picked up in-progress session {sid}")

            # ── Session in progress: update ────────────────────────────────
            elif is_charging and session_id:
                if energy_wh > 0:
                    poller_state["session_energy"] = energy_wh

                start_dt   = poller_state["session_start"]
                local_dur  = int((now_utc - start_dt).total_seconds()) if start_dt else 0
                duration   = session_s if session_s > 0 else local_dur

                db_exec(
                    "UPDATE sessions SET end_time=?, duration_s=?, energy_wh=? WHERE id=?",
                    (now_iso, duration, poller_state["session_energy"], session_id)
                )
                db_exec(
                    "INSERT INTO session_samples (session_id, ts, energy_wh, current_a, grid_v) VALUES (?,?,?,?,?)",
                    (session_id, now_iso, energy_wh, v.get("vehicle_current_a"), v.get("grid_v"))
                )

                # Auto-tag vehicle once we have 2+ minutes of data
                if not poller_state["auto_tagged"] and duration >= AUTO_TAG_AFTER_S and poller_state["session_energy"] > 0:
                    avg_w   = poller_state["session_energy"] / (duration / 3600)
                    vehicle = detect_vehicle(avg_w)
                    start   = db_one("SELECT start_time FROM sessions WHERE id=?", (session_id,))
                    rate    = rate_for_vehicle(vehicle, start["start_time"])
                    db_exec(
                        "UPDATE sessions SET vehicle=?, auto_tagged=1, rate_kwh=? WHERE id=?",
                        (vehicle, rate, session_id)
                    )
                    poller_state["auto_tagged"] = True
                    print(f"[{now_utc.strftime('%H:%M:%S')}] Session {session_id} → {vehicle}  {avg_w:.0f}W  ${rate:.4f}/kWh")

            # ── Session ended ──────────────────────────────────────────────
            elif not is_charging and was_charging and session_id:
                start_dt     = poller_state["session_start"]
                local_dur    = int((now_utc - start_dt).total_seconds()) if start_dt else 0
                duration     = session_s if session_s > 0 else local_dur
                final_energy = energy_wh if energy_wh > 0 else poller_state["session_energy"]

                db_exec(
                    "UPDATE sessions SET end_time=?, duration_s=?, energy_wh=? WHERE id=?",
                    (now_iso, duration, final_energy, session_id)
                )
                row  = db_one("SELECT * FROM sessions WHERE id=?", (session_id,))
                cost = session_cost(row)
                print(f"[{now_utc.strftime('%H:%M:%S')}] Session {session_id} ended — "
                      f"{(row['energy_wh'] or 0)/1000:.2f} kWh  "
                      f"{fmt_duration(row['duration_s'])}  ${cost:.2f}  [{row['vehicle']}]")

                poller_state.update(session_id=None, session_energy=0.0,
                                    session_start=None, auto_tagged=False)

            poller_state["was_charging"] = is_charging

        time.sleep(POLL_S)


# ── REST API ──────────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    with state_lock:
        vitals    = poller_state["last_vitals"]
        poll_err  = poller_state["poll_error"]
        poll_ts   = poller_state["last_poll_ts"]
        sess_id   = poller_state["session_id"]
        sess_nrg  = poller_state["session_energy"]
        sess_st   = poller_state["session_start"]

    lt   = fetch_json(f"http://{WC_IP}/api/1/lifetime")
    ver  = fetch_json(f"http://{WC_IP}/api/1/version")
    wifi = fetch_json(f"http://{WC_IP}/api/1/wifi_status")

    if wifi and wifi.get("wifi_ssid"):
        try:
            wifi["wifi_ssid_decoded"] = base64.b64decode(wifi["wifi_ssid"]).decode("utf-8")
        except Exception:
            wifi["wifi_ssid_decoded"] = wifi["wifi_ssid"]

    current_session = None
    if sess_id:
        row = db_one("SELECT * FROM sessions WHERE id=?", (sess_id,))
        if row:
            current_session = {**row, "cost": session_cost(row)}

    return jsonify({
        "ok":              not poll_err,
        "last_poll":       poll_ts,
        "vitals":          vitals,
        "lifetime":        lt,
        "version":         ver,
        "wifi":            wifi,
        "current_session": current_session,
    })


@app.route("/api/sessions")
def api_sessions():
    where, params = [], []

    days    = request.args.get("days",    type=int)
    month   = request.args.get("month")
    vehicle = request.args.get("vehicle")

    if days:
        where.append("start_time >= datetime('now', ?)")
        params.append(f"-{days} days")
    if month:
        where.append("strftime('%Y-%m', start_time) = ?")
        params.append(month)
    if vehicle:
        where.append("lower(vehicle) = lower(?)")
        params.append(vehicle)

    sql = "SELECT * FROM sessions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY start_time DESC"

    rows = db_query(sql, params)
    for r in rows:
        r["cost"]         = session_cost(r)
        r["duration_fmt"] = fmt_duration(r.get("duration_s"))
    return jsonify(rows)


@app.route("/api/sessions/<int:sid>", methods=["GET"])
def api_session_get(sid):
    row = db_one("SELECT * FROM sessions WHERE id=?", (sid,))
    if not row:
        return jsonify({"error": "not found"}), 404
    row["cost"] = session_cost(row)
    return jsonify(row)


@app.route("/api/sessions/<int:sid>", methods=["PATCH"])
def api_session_patch(sid):
    row = db_one("SELECT * FROM sessions WHERE id=?", (sid,))
    if not row:
        return jsonify({"error": "not found"}), 404

    body    = request.get_json(force=True)
    updates = []
    params  = []

    if "vehicle" in body:
        vehicle = body["vehicle"]
        updates.append("vehicle=?")
        params.append(vehicle)
        updates.append("auto_tagged=0")
        # Recalculate rate when vehicle changes
        rate = rate_for_vehicle(vehicle, row["start_time"])
        updates.append("rate_kwh=?")
        params.append(rate)

    if "notes" in body:
        updates.append("notes=?")
        params.append(body["notes"])

    if "rate_kwh" in body:
        updates.append("rate_kwh=?")
        params.append(float(body["rate_kwh"]))

    if updates:
        params.append(sid)
        db_exec(f"UPDATE sessions SET {', '.join(updates)} WHERE id=?", params)

    row = db_one("SELECT * FROM sessions WHERE id=?", (sid,))
    row["cost"] = session_cost(row)
    return jsonify(row)


@app.route("/api/sessions/<int:sid>/samples")
def api_session_samples(sid):
    rows = db_query(
        "SELECT ts, energy_wh, current_a, grid_v FROM session_samples WHERE session_id=? ORDER BY ts",
        (sid,)
    )
    # Compute instantaneous power from consecutive energy readings
    for i in range(len(rows)):
        if i == 0:
            rows[i]["power_w"] = None
            continue
        prev = rows[i - 1]
        try:
            dt = (datetime.fromisoformat(rows[i]["ts"]) -
                  datetime.fromisoformat(prev["ts"])).total_seconds()
            de = (rows[i]["energy_wh"] or 0) - (prev["energy_wh"] or 0)
            rows[i]["power_w"] = round(de / (dt / 3600)) if dt > 0 and de >= 0 else None
        except Exception:
            rows[i]["power_w"] = None
    return jsonify(rows)


@app.route("/api/config", methods=["GET"])
def api_config_get():
    return jsonify({"wc_ip": WC_IP, **CONFIG})


@app.route("/api/config", methods=["PATCH"])
def api_config_patch():
    body = request.get_json(force=True)
    with CONFIG_LOCK:
        for key in ("rate_general_kwh", "rate_ev_powerup_kwh",
                    "offpeak_start_hour", "offpeak_end_hour"):
            if key in body:
                CONFIG[key] = body[key]
        if "vehicles" in body:
            CONFIG["vehicles"] = body["vehicles"]
        save_config()
    return jsonify({"ok": True, **CONFIG})


@app.route("/api/lifetime")
def api_lifetime():
    lt = fetch_json(f"http://{WC_IP}/api/1/lifetime")
    return jsonify(lt or {"error": "unreachable"})


@app.route("/api/summary")
def api_summary():
    rows = db_query("SELECT vehicle, COUNT(*) as count, SUM(energy_wh) as total_wh, "
                    "SUM(energy_wh / 1000.0 * rate_kwh) as total_cost "
                    "FROM sessions WHERE energy_wh IS NOT NULL GROUP BY vehicle")
    totals = db_one("SELECT COUNT(*) as sessions, SUM(energy_wh) as total_wh, "
                    "SUM(energy_wh / 1000.0 * rate_kwh) as total_cost FROM sessions WHERE energy_wh IS NOT NULL")
    return jsonify({"by_vehicle": rows, "totals": totals})


# ── Web dashboard ─────────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wall Connector</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#0d0d0d;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:14px}
  header{padding:14px 24px;border-bottom:1px solid #1e1e1e;display:flex;justify-content:space-between;align-items:center}
  header h1{font-size:15px;font-weight:600;letter-spacing:.03em;color:#fff}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
  .dot.green{background:#00c853}
  .dot.red{background:#f44336}
  .dot.amber{background:#ff9800}
  #poll-ts{font-size:11px;color:#555}
  .container{max-width:1100px;margin:0 auto;padding:20px 24px}

  /* Status card */
  .status-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:20px}
  .stat{background:#111;border:1px solid #1e1e1e;border-radius:8px;padding:14px 16px}
  .stat-label{font-size:11px;color:#555;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px}
  .stat-value{font-size:18px;font-weight:600;color:#fff}
  .stat-value.green{color:#00c853}
  .stat-value.amber{color:#ff9800}
  .stat-value.dim{color:#888;font-size:14px}
  .badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600}
  .badge.tesla{background:#1a3a5c;color:#64b5f6}
  .badge.shark{background:#3b2f00;color:#ffd54f}
  .badge.charging{background:#003320;color:#00c853}
  .badge.idle{background:#1e1e1e;color:#666}

  /* Session table */
  .toolbar{display:flex;gap:8px;align-items:center;margin-bottom:16px;flex-wrap:wrap}
  .btn{background:#1a1a1a;border:1px solid #2a2a2a;color:#ccc;padding:6px 14px;border-radius:5px;cursor:pointer;font-size:12px;transition:all .15s}
  .btn:hover{border-color:#444;color:#fff}
  .btn.active{background:#1a3a5c;border-color:#1e5a9c;color:#64b5f6}
  select.filter{background:#1a1a1a;border:1px solid #2a2a2a;color:#ccc;padding:5px 8px;border-radius:5px;font-size:12px;cursor:pointer}
  .spacer{flex:1}
  table{width:100%;border-collapse:collapse}
  th{text-align:left;padding:8px 12px;font-size:11px;color:#555;text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid #1e1e1e;white-space:nowrap}
  td{padding:8px 12px;border-bottom:1px solid #161616;vertical-align:middle}
  tr:hover td{background:#111}
  td.num{text-align:right;font-variant-numeric:tabular-nums}
  .vehicle-select{background:#1a1a1a;border:1px solid #2a2a2a;color:#ccc;padding:3px 6px;border-radius:4px;font-size:12px;cursor:pointer;min-width:80px}
  .vehicle-select.tesla{background:#1a3a5c;border-color:#1e5a9c;color:#64b5f6}
  .vehicle-select.shark{background:#3b2f00;border-color:#6b5200;color:#ffd54f}
  .note-input{background:#1a1a1a;border:1px solid #2a2a2a;color:#ccc;padding:3px 6px;border-radius:4px;font-size:12px;width:160px}
  .auto-tag{font-size:10px;color:#555;margin-left:4px}
  .total-row td{border-top:1px solid #2a2a2a;border-bottom:none;font-weight:600;color:#fff}
  .summary-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-top:20px}
  .sum-card{background:#111;border:1px solid #1e1e1e;border-radius:7px;padding:12px 16px}
  .sum-label{font-size:11px;color:#555;margin-bottom:4px}
  .sum-val{font-size:16px;font-weight:600;color:#fff}
  .offpeak-note{font-size:11px;color:#555;margin-top:6px;text-align:center}
  .live-section{background:#111;border:1px solid #1e3a1e;border-radius:8px;padding:16px 20px;margin-bottom:20px;display:none}
  .live-header{display:flex;align-items:center;gap:10px;margin-bottom:12px}
  .live-title{font-size:12px;font-weight:600;color:#00c853}
  .live-sub{font-size:11px;color:#555}
  .live-chart-wrap{height:200px;position:relative}
  .trend-btn{background:none;border:1px solid #2a2a2a;color:#555;padding:2px 7px;border-radius:3px;cursor:pointer;font-size:11px}
  .trend-btn:hover{border-color:#444;color:#aaa}
  #chart-modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.88);z-index:100;align-items:center;justify-content:center}
  .modal-box{background:#111;border:1px solid #222;border-radius:10px;padding:24px;width:min(820px,92vw);position:relative}
  .modal-close{position:absolute;top:12px;right:16px;background:none;border:none;color:#666;font-size:18px;cursor:pointer;line-height:1}
  .modal-close:hover{color:#ccc}
  .modal-title{color:#fff;font-size:13px;font-weight:600;margin-bottom:4px}
  .modal-sub{font-size:11px;color:#555;margin-bottom:16px}
  .chart-wrap{height:300px;position:relative}
  .modal-toggle{display:flex;gap:6px;margin-bottom:12px}
</style>
</head>
<body>
<header>
  <h1><span class="dot green" id="conn-dot"></span>Wall Connector</h1>
  <div style="display:flex;gap:16px;align-items:center">
    <span id="poll-ts" style="font-size:11px;color:#555">–</span>
    <a href="/settings" style="font-size:12px;color:#555;text-decoration:none">Settings</a>
  </div>
</header>

<div class="container">
  <!-- Status -->
  <div class="status-grid" id="status-grid">
    <div class="stat"><div class="stat-label">Grid</div><div class="stat-value dim" id="s-grid">–</div></div>
    <div class="stat"><div class="stat-label">State</div><div class="stat-value" id="s-state">–</div></div>
    <div class="stat"><div class="stat-label">Session energy</div><div class="stat-value green" id="s-energy">–</div></div>
    <div class="stat"><div class="stat-label">Session duration</div><div class="stat-value" id="s-dur">–</div></div>
    <div class="stat"><div class="stat-label">Avg power</div><div class="stat-value" id="s-power">–</div></div>
    <div class="stat"><div class="stat-label">Est. cost</div><div class="stat-value" id="s-cost">–</div></div>
    <div class="stat"><div class="stat-label">WiFi</div><div class="stat-value dim" id="s-wifi">–</div></div>
    <div class="stat"><div class="stat-label">Firmware</div><div class="stat-value dim" id="s-firmware">–</div></div>
  </div>

  <!-- Live charge section -->
  <div class="live-section" id="live-section">
    <div class="live-header">
      <span class="dot green"></span>
      <span class="live-title" id="live-title">Charging now</span>
      <span class="live-sub" id="live-sub"></span>
      <div style="flex:1"></div>
      <button class="btn" id="live-btn-energy" onclick="switchLiveAxis('energy')">vs Energy</button>
      <button class="btn" id="live-btn-time"   onclick="switchLiveAxis('time')">vs Time</button>
    </div>
    <div class="live-chart-wrap"><canvas id="live-chart"></canvas></div>
  </div>

  <!-- Toolbar -->
  <div class="toolbar">
    <button class="btn active" onclick="setDays(7)">7 days</button>
    <button class="btn" onclick="setDays(30)">30 days</button>
    <button class="btn" onclick="setDays(90)">90 days</button>
    <button class="btn" onclick="setDays(null)">All</button>
    <select class="filter" id="vehicle-filter" onchange="setVehicle(this.value)">
      <option value="">All vehicles</option>
    </select>
    <div class="spacer"></div>
    <span style="font-size:11px;color:#444" id="session-count">–</span>
  </div>

  <!-- Table -->
  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>Date</th>
        <th>Start</th>
        <th>Vehicle</th>
        <th class="num">kWh</th>
        <th class="num">Avg W</th>
        <th class="num">Duration</th>
        <th class="num">Rate</th>
        <th class="num">Cost</th>
        <th>Notes</th>
        <th></th>
      </tr>
    </thead>
    <tbody id="sessions-tbody"></tbody>
    <tfoot id="sessions-tfoot"></tfoot>
  </table>

  <!-- Summary -->
  <div class="summary-grid" id="summary-grid"></div>
  <p class="offpeak-note" id="rate-note">~ = auto-tagged by power draw</p>
</div>

<script>
let currentDays    = 7;
let currentVehicle = '';
let vehicleNames   = ['Unknown'];
let liveSessionId  = null;
let liveAxis       = 'energy';
let liveChart      = null;

function setDays(d) {
  currentDays = d;
  document.querySelectorAll('.toolbar .btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  loadSessions();
}
function setVehicle(v) { currentVehicle = v; loadSessions(); }

async function loadStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    const v = d.vitals || {};
    const cs = d.current_session;

    document.getElementById('conn-dot').className = 'dot ' + (d.ok ? 'green' : 'red');
    document.getElementById('poll-ts').textContent = d.last_poll
      ? 'Updated ' + new Date(d.last_poll).toLocaleTimeString() : '–';

    document.getElementById('s-grid').textContent =
      v.grid_v ? `${v.grid_v.toFixed(1)}V / ${v.grid_hz ? v.grid_hz.toFixed(2) : '–'}Hz` : '–';

    if (v.contactor_closed) {
      document.getElementById('s-state').innerHTML = '<span class="badge charging">Charging</span>';
    } else if (v.vehicle_connected) {
      document.getElementById('s-state').innerHTML = '<span class="badge idle">Connected</span>';
    } else {
      document.getElementById('s-state').innerHTML = '<span class="badge idle">Idle</span>';
    }

    const wh = cs ? cs.energy_wh : (v.session_energy_wh || 0);
    const dur = cs ? cs.duration_s : (v.session_s || 0);
    document.getElementById('s-energy').textContent = wh ? (wh / 1000).toFixed(2) + ' kWh' : '–';
    document.getElementById('s-dur').textContent = dur ? fmtDur(dur) : '–';

    const avgW = (wh && dur > 10) ? (wh / (dur / 3600)) : null;
    document.getElementById('s-power').textContent = avgW ? Math.round(avgW) + ' W' : '–';

    if (cs) {
      document.getElementById('s-cost').textContent = '$' + cs.cost.toFixed(2);
      // Drive live section
      const liveEl = document.getElementById('live-section');
      liveEl.style.display = 'block';
      document.getElementById('live-title').textContent =
        `Session #${cs.id} — ${cs.vehicle || 'detecting…'}`;
      document.getElementById('live-sub').textContent =
        `${cs.energy_wh ? (cs.energy_wh/1000).toFixed(2)+' kWh' : '–'}`;
      if (cs.id !== liveSessionId) {
        liveSessionId = cs.id;
        if (liveChart) { liveChart.destroy(); liveChart = null; }
      }
      loadLiveSamples(cs.id);
    } else {
      document.getElementById('s-cost').textContent = '–';
      document.getElementById('live-section').style.display = 'none';
    }

    const wifi = d.wifi || {};
    const wifiEl = document.getElementById('s-wifi');
    if (wifi.wifi_connected) {
      const rssi = wifi.wifi_rssi || 0;
      const strength = rssi >= -50 ? 'Excellent' : rssi >= -65 ? 'Good' : rssi >= -75 ? 'Fair' : 'Weak';
      const inet = wifi.internet ? '' : ' · No internet';
      wifiEl.textContent = `${strength} (${rssi} dBm)${inet}`;
      wifiEl.title = wifi.wifi_ssid_decoded || '';
    } else {
      wifiEl.textContent = 'Disconnected';
    }

    const ver = d.version || {};
    const fwEl = document.getElementById('s-firmware');
    if (ver.firmware_version) {
      fwEl.textContent = ver.firmware_version.split('+')[0];
      fwEl.title = ver.firmware_version;
    }
  } catch(e) {
    document.getElementById('conn-dot').className = 'dot red';
  }
}

async function loadSessions() {
  let url = '/api/sessions';
  const params = [];
  if (currentDays) params.push('days=' + currentDays);
  if (currentVehicle) params.push('vehicle=' + encodeURIComponent(currentVehicle));
  if (params.length) url += '?' + params.join('&');

  const r    = await fetch(url);
  const rows = await r.json();

  document.getElementById('session-count').textContent =
    rows.length === 1 ? '1 session' : rows.length + ' sessions';

  const tbody = document.getElementById('sessions-tbody');
  tbody.innerHTML = '';

  let totalWh = 0, totalCost = 0;

  for (const row of rows) {
    const dt = row.start_time ? new Date(row.start_time) : null;
    const dateStr = dt ? dt.toLocaleDateString('en-AU') : '–';
    const timeStr = dt ? dt.toLocaleTimeString('en-AU', {hour:'2-digit', minute:'2-digit'}) : '–';
    const wh = row.energy_wh || 0;
    const dur = row.duration_s || 0;
    const avgW = (wh && dur > 10) ? Math.round(wh / (dur / 3600)) : null;
    const vehicle = row.vehicle || 'Unknown';
    const vClass = vehicle === 'Tesla' ? 'tesla' : vehicle === 'Shark' ? 'shark' : '';

    totalWh += wh;
    totalCost += row.cost || 0;

    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td style="color:#444;font-size:12px">${row.id}</td>
      <td>${dateStr}</td>
      <td>${timeStr}</td>
      <td>
        <select class="vehicle-select ${vClass}" onchange="patchSession(${row.id}, 'vehicle', this.value, this)">
          ${vehicleNames.map(n => `<option ${vehicle===n?'selected':''}>${n}</option>`).join('')}
          ${vehicleNames.includes(vehicle) ? '' : `<option selected>${vehicle}</option>`}
        </select>
        ${row.auto_tagged ? '<span class="auto-tag">~</span>' : ''}
      </td>
      <td class="num">${wh ? (wh/1000).toFixed(2) : '–'}</td>
      <td class="num">${avgW ?? '–'}</td>
      <td class="num">${dur ? fmtDur(dur) : '–'}</td>
      <td class="num" style="font-size:11px;color:#555">$${(row.rate_kwh||0).toFixed(4)}</td>
      <td class="num">$${(row.cost||0).toFixed(2)}</td>
      <td>
        <input class="note-input" value="${(row.notes||'').replace(/"/g,'&quot;')}"
          onblur="patchSession(${row.id}, 'notes', this.value, null)"
          onkeydown="if(event.key==='Enter')this.blur()">
      </td>
      <td><button class="trend-btn" onclick="showChart(${row.id}, '${vehicle}', ${wh/1000})">Trend</button></td>`;
    tbody.appendChild(tr);
  }

  // Total row
  const tfoot = document.getElementById('sessions-tfoot');
  tfoot.innerHTML = `<tr class="total-row">
    <td colspan="4">Total</td>
    <td class="num">${(totalWh/1000).toFixed(2)}</td>
    <td colspan="3"></td>
    <td class="num">$${totalCost.toFixed(2)}</td>
    <td></td>
  </tr>`;

  loadSummary();
}

async function loadSummary() {
  const r    = await fetch('/api/summary');
  const data = await r.json();
  const grid = document.getElementById('summary-grid');
  grid.innerHTML = '';

  const totals = data.totals || {};
  addCard(grid, 'All time sessions', (totals.sessions || 0).toLocaleString(), '');
  addCard(grid, 'All time energy', totals.total_wh ? (totals.total_wh/1000).toFixed(1) + ' kWh' : '–', '');
  addCard(grid, 'All time cost', totals.total_cost ? '$' + totals.total_cost.toFixed(2) : '–', '');

  for (const v of (data.by_vehicle || [])) {
    if (!v.vehicle || v.vehicle === 'Unknown') continue;
    const vclass = v.vehicle === 'Tesla' ? 'tesla' : 'shark';
    addCard(grid,
      `<span class="badge ${vclass}">${v.vehicle}</span>`,
      `${v.count} sessions · ${v.total_wh ? (v.total_wh/1000).toFixed(1) + ' kWh' : '–'}`,
      v.total_cost ? '$' + v.total_cost.toFixed(2) : '–'
    );
  }
}

function addCard(grid, label, value, sub) {
  const div = document.createElement('div');
  div.className = 'sum-card';
  div.innerHTML = `<div class="sum-label">${label}</div>
    <div class="sum-val">${value}</div>
    ${sub ? `<div style="font-size:12px;color:#666;margin-top:4px">${sub}</div>` : ''}`;
  grid.appendChild(div);
}

async function patchSession(id, field, value, selectEl) {
  const payload = {};
  payload[field] = value;
  const r = await fetch(`/api/sessions/${id}`, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  const updated = await r.json();
  // Update dropdown styling if vehicle changed
  if (field === 'vehicle' && selectEl) {
    selectEl.className = 'vehicle-select ' +
      (value === 'Tesla' ? 'tesla' : value === 'Shark' ? 'shark' : '');
  }
  // Update rate cell in same row
  if (field === 'vehicle') {
    const row = selectEl.closest('tr');
    if (row) {
      const rateCell = row.querySelectorAll('td')[7];
      if (rateCell) rateCell.textContent = '$' + (updated.rate_kwh||0).toFixed(4);
      const costCell = row.querySelectorAll('td')[8];
      if (costCell) costCell.textContent = '$' + (updated.cost||0).toFixed(2);
    }
  }
}

function fmtDur(s) {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${String(m).padStart(2,'0')}m` : `${m}m`;
}

// ── Live chart ────────────────────────────────────────────────────────────────
async function loadLiveSamples(sid) {
  try {
    const r       = await fetch(`/api/sessions/${sid}/samples`);
    const samples = await r.json();
    renderLiveChart(samples);
  } catch(e) {}
}

function switchLiveAxis(axis) {
  liveAxis = axis;
  document.getElementById('live-btn-energy').classList.toggle('active', axis === 'energy');
  document.getElementById('live-btn-time').classList.toggle('active', axis === 'time');
}

function renderLiveChart(samples) {
  const pts = samples.filter(s => s.power_w !== null && s.power_w > 0);
  if (!pts.length) return;

  const base = samples[0];
  let labels, xLabel;
  if (liveAxis === 'energy') {
    labels = pts.map(s => ((s.energy_wh - (base.energy_wh||0)) / 1000).toFixed(2));
    xLabel = 'Energy delivered (kWh)';
  } else {
    const t0 = new Date(base.ts);
    labels = pts.map(s => ((new Date(s.ts) - t0) / 60000).toFixed(1));
    xLabel = 'Time (min)';
  }

  const ctx = document.getElementById('live-chart').getContext('2d');
  if (liveChart) {
    liveChart.data.labels = labels;
    liveChart.data.datasets[0].data = pts.map(s => s.power_w);
    liveChart.options.scales.x.title.text = xLabel;
    liveChart.update('none');
    return;
  }
  liveChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        data: pts.map(s => s.power_w),
        borderColor: '#00c853',
        backgroundColor: 'rgba(0,200,83,0.06)',
        borderWidth: 2,
        tension: 0.35,
        pointRadius: 2,
        pointHoverRadius: 4,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#1a1a1a', borderColor: '#333', borderWidth: 1,
          titleColor: '#ccc', bodyColor: '#ccc',
          callbacks: { label: ctx => `${(ctx.parsed.y/1000).toFixed(2)} kW` }
        }
      },
      scales: {
        x: { title: { display: true, text: xLabel, color: '#555', font: {size:11} },
             ticks: { color: '#555', maxTicksLimit: 8 }, grid: { color: '#1a1a1a' } },
        y: { title: { display: true, text: 'Power (W)', color: '#555', font: {size:11} },
             ticks: { color: '#555', callback: v => v >= 1000 ? (v/1000).toFixed(1)+'k' : v },
             grid: { color: '#1a1a1a' }, min: 0 }
      }
    }
  });
}

// ── Trend chart ───────────────────────────────────────────────────────────────
let chartInstance = null;
let chartSamples  = [];
let chartAxis     = 'energy';
let chartMeta     = {};

async function showChart(sessionId, vehicle, totalKwh) {
  const r = await fetch(`/api/sessions/${sessionId}/samples`);
  chartSamples = await r.json();

  chartMeta = { sessionId, vehicle, totalKwh };
  document.getElementById('chart-modal').style.display = 'flex';
  document.getElementById('chart-title').textContent =
    `Session #${sessionId} — ${vehicle} — ${totalKwh.toFixed(2)} kWh`;

  chartAxis = 'energy';
  document.getElementById('btn-energy').classList.add('active');
  document.getElementById('btn-time').classList.remove('active');
  renderChart();
}

function switchAxis(axis) {
  chartAxis = axis;
  document.getElementById('btn-energy').classList.toggle('active', axis === 'energy');
  document.getElementById('btn-time').classList.toggle('active', axis === 'time');
  renderChart();
}

function renderChart() {
  const samples = chartSamples.filter(s => s.power_w !== null && s.power_w > 0);
  if (!samples.length) {
    document.getElementById('chart-sub').textContent = 'No sample data for this session.';
    if (chartInstance) { chartInstance.destroy(); chartInstance = null; }
    return;
  }

  // X-axis: energy delivered (kWh) or elapsed time (min)
  let labels, xLabel;
  if (chartAxis === 'energy') {
    // Offset from session start energy
    const baseEnergy = chartSamples[0].energy_wh || 0;
    labels = samples.map(s => ((s.energy_wh - baseEnergy) / 1000).toFixed(2));
    xLabel = 'Energy Delivered (kWh) — SOC proxy';
  } else {
    const t0 = new Date(chartSamples[0].ts);
    labels = samples.map(s => ((new Date(s.ts) - t0) / 60000).toFixed(1));
    xLabel = 'Time (minutes)';
  }

  const powers = samples.map(s => s.power_w);
  const maxP   = Math.max(...powers);
  document.getElementById('chart-sub').textContent =
    `${samples.length} samples · peak ${(maxP/1000).toFixed(1)} kW`;

  const ctx = document.getElementById('trend-chart').getContext('2d');
  if (chartInstance) chartInstance.destroy();
  chartInstance = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: 'Power (W)',
        data: powers,
        borderColor: '#00c853',
        backgroundColor: 'rgba(0,200,83,0.08)',
        borderWidth: 2,
        tension: 0.35,
        pointRadius: 2,
        pointHoverRadius: 5,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#1a1a1a',
          borderColor: '#333',
          borderWidth: 1,
          titleColor: '#ccc',
          bodyColor: '#ccc',
          callbacks: {
            label: ctx => `${(ctx.parsed.y/1000).toFixed(2)} kW`
          }
        }
      },
      scales: {
        x: {
          title: { display: true, text: xLabel, color: '#555', font: { size: 11 } },
          ticks: { color: '#555', maxTicksLimit: 10 },
          grid:  { color: '#1a1a1a' }
        },
        y: {
          title: { display: true, text: 'Power (W)', color: '#555', font: { size: 11 } },
          ticks: { color: '#555', callback: v => v >= 1000 ? (v/1000).toFixed(1)+'k' : v },
          grid:  { color: '#1a1a1a' },
          min:   0
        }
      }
    }
  });
}

function closeChart() {
  document.getElementById('chart-modal').style.display = 'none';
  if (chartInstance) { chartInstance.destroy(); chartInstance = null; }
}

// Close modal on backdrop click
document.getElementById('chart-modal').addEventListener('click', e => {
  if (e.target === document.getElementById('chart-modal')) closeChart();
});

async function loadConfig() {
  try {
    const r = await fetch('/api/config');
    const c = await r.json();

    // Populate vehicle names for dropdowns
    vehicleNames = (c.vehicles || []).map(v => v.name);
    if (!vehicleNames.includes('Unknown')) vehicleNames.push('Unknown');

    const filter = document.getElementById('vehicle-filter');
    const prev   = filter.value;
    filter.innerHTML = '<option value="">All vehicles</option>' +
      vehicleNames.map(n => `<option value="${n}">${n}</option>`).join('');
    filter.value = prev;

    // Footer note
    const note = document.getElementById('rate-note');
    if (note && c.vehicles) {
      const s = c.offpeak_start_hour, e = c.offpeak_end_hour;
      const fmtH = h => h === 0 ? '12am' : h < 12 ? `${h}am` : h === 12 ? '12pm' : `${h-12}pm`;
      const evVehicles = c.vehicles.filter(v => v.ev_powerup).map(v => v.name).join(', ');
      note.textContent =
        `~ = auto-tagged  •  ` +
        (evVehicles ? `${evVehicles} off-peak (${fmtH(s)}–${fmtH(e)}): $${c.rate_ev_powerup_kwh.toFixed(3)}/kWh  •  ` : '') +
        `General: $${c.rate_general_kwh.toFixed(4)}/kWh`;
    }
  } catch(e) {}
}

// Initial load + periodic refresh
loadConfig();
loadStatus();
loadSessions();
setInterval(loadStatus, 30000);
setInterval(loadSessions, 60000);
</script>
<div id="chart-modal">
  <div class="modal-box">
    <button class="modal-close" onclick="closeChart()">✕</button>
    <div class="modal-title" id="chart-title"></div>
    <div class="modal-sub" id="chart-sub"></div>
    <div class="modal-toggle">
      <button class="btn active" id="btn-energy" onclick="switchAxis('energy')">Power vs Energy</button>
      <button class="btn" id="btn-time" onclick="switchAxis('time')">Power vs Time</button>
    </div>
    <div class="chart-wrap"><canvas id="trend-chart"></canvas></div>
    <p style="font-size:11px;color:#333;margin-top:10px;text-align:center">
      Calculated from 30-second energy samples · X-axis approximates SOC progression
    </p>
  </div>
</div>
</body>
</html>"""


SETTINGS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wall Connector — Settings</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#0d0d0d;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:14px}
  header{padding:14px 24px;border-bottom:1px solid #1e1e1e;display:flex;align-items:center;gap:16px}
  a.back{color:#555;text-decoration:none;font-size:13px}a.back:hover{color:#ccc}
  h1{font-size:15px;font-weight:600;color:#fff}
  .container{max-width:700px;margin:0 auto;padding:24px}
  h2{font-size:12px;text-transform:uppercase;letter-spacing:.07em;color:#555;margin:28px 0 12px}
  .card{background:#111;border:1px solid #1e1e1e;border-radius:8px;padding:16px 20px}
  .field{display:grid;grid-template-columns:220px 1fr;gap:8px;align-items:center;padding:8px 0;border-bottom:1px solid #161616}
  .field:last-child{border-bottom:none}
  .field label{font-size:12px;color:#888}
  input[type=text],input[type=number]{background:#1a1a1a;border:1px solid #2a2a2a;color:#e0e0e0;padding:6px 10px;border-radius:4px;font-size:13px;width:100%}
  input:focus{outline:none;border-color:#444}
  .vehicle-row{display:grid;grid-template-columns:1fr 110px 90px 32px;gap:8px;align-items:center;margin-bottom:8px}
  .vehicle-row .col-label{font-size:11px;color:#555;padding-bottom:4px}
  .check-wrap{display:flex;align-items:center;gap:6px;font-size:12px;color:#888}
  input[type=checkbox]{accent-color:#00c853;width:15px;height:15px;cursor:pointer}
  .del-btn{background:#1a1a1a;border:1px solid #2a2a2a;color:#666;width:28px;height:28px;border-radius:4px;cursor:pointer;font-size:16px;line-height:1;display:flex;align-items:center;justify-content:center}
  .del-btn:hover{border-color:#c00;color:#f44}
  .add-btn{background:#1a1a1a;border:1px solid #2a2a2a;color:#888;padding:6px 14px;border-radius:4px;cursor:pointer;font-size:12px;margin-top:8px}
  .add-btn:hover{border-color:#444;color:#ccc}
  .save-btn{background:#1a3a5c;border:1px solid #1e5a9c;color:#64b5f6;padding:8px 24px;border-radius:5px;cursor:pointer;font-size:13px;font-weight:600;margin-top:20px}
  .save-btn:hover{background:#1e4a7c}
  .status{font-size:12px;color:#00c853;margin-top:8px;min-height:18px}
  .note{font-size:11px;color:#444;margin-top:6px}
</style>
</head>
<body>
<header>
  <a class="back" href="/">← Dashboard</a>
  <h1>Settings</h1>
</header>
<div class="container">

  <h2>Vehicles</h2>
  <p class="note" style="margin-bottom:12px">
    Vehicle is auto-detected by average charge power after 2 minutes.
    The lowest-capacity vehicle whose max power × 1.2 ≥ observed power is chosen.
  </p>
  <div class="card">
    <div class="vehicle-row" style="margin-bottom:2px">
      <span class="col-label">Name</span>
      <span class="col-label">Max power (W)</span>
      <span class="col-label">EV off-peak rate</span>
      <span></span>
    </div>
    <div id="vehicles-list"></div>
    <button class="add-btn" onclick="addVehicle()">+ Add vehicle</button>
  </div>

  <h2>Electricity Rates</h2>
  <div class="card">
    <div class="field">
      <label>General rate ($/kWh)</label>
      <input type="number" id="rate_general" step="0.0001" min="0">
    </div>
    <div class="field">
      <label>EV off-peak rate ($/kWh)</label>
      <input type="number" id="rate_ev_powerup" step="0.0001" min="0">
    </div>
  </div>

  <h2>Off-peak Window (local time)</h2>
  <div class="card">
    <div class="field">
      <label>Start hour (0–23)</label>
      <input type="number" id="offpeak_start" min="0" max="23">
    </div>
    <div class="field">
      <label>End hour (0–23)</label>
      <input type="number" id="offpeak_end" min="0" max="23">
    </div>
  </div>
  <p class="note">e.g. Start 22, End 7 = 10pm to 7am. Applied to vehicles with EV off-peak rate enabled.</p>

  <button class="save-btn" onclick="saveConfig()">Save</button>
  <div class="status" id="status"></div>

  <h2>Charger</h2>
  <div class="card">
    <div class="field">
      <label>Wall Connector IP</label>
      <input type="text" id="wc_ip" disabled style="color:#555">
    </div>
  </div>
  <p class="note">IP address requires a server restart to change (edit config.json).</p>
</div>

<script>
let cfg = {};

async function load() {
  const r = await fetch('/api/config');
  cfg = await r.json();
  document.getElementById('rate_general').value   = cfg.rate_general_kwh    || 0;
  document.getElementById('rate_ev_powerup').value = cfg.rate_ev_powerup_kwh || 0;
  document.getElementById('offpeak_start').value  = cfg.offpeak_start_hour  ?? 22;
  document.getElementById('offpeak_end').value    = cfg.offpeak_end_hour    ?? 7;
  document.getElementById('wc_ip').value          = cfg.wc_ip || '';
  renderVehicles(cfg.vehicles || []);
}

function renderVehicles(vehicles) {
  const list = document.getElementById('vehicles-list');
  list.innerHTML = '';
  vehicles.forEach((v, i) => {
    const row = document.createElement('div');
    row.className = 'vehicle-row';
    row.dataset.i = i;
    row.innerHTML = `
      <input type="text" value="${v.name || ''}" oninput="updateV(${i},'name',this.value)" placeholder="Vehicle name">
      <input type="number" value="${v.max_power_w || ''}" oninput="updateV(${i},'max_power_w',+this.value)" placeholder="Watts" min="0">
      <div class="check-wrap">
        <input type="checkbox" ${v.ev_powerup ? 'checked' : ''} onchange="updateV(${i},'ev_powerup',this.checked)">
        off-peak
      </div>
      <button class="del-btn" onclick="removeV(${i})">×</button>`;
    list.appendChild(row);
  });
}

function updateV(i, key, val) {
  cfg.vehicles[i][key] = val;
}
function removeV(i) {
  cfg.vehicles.splice(i, 1);
  renderVehicles(cfg.vehicles);
}
function addVehicle() {
  if (!cfg.vehicles) cfg.vehicles = [];
  cfg.vehicles.push({name: '', max_power_w: 0, ev_powerup: false});
  renderVehicles(cfg.vehicles);
}

async function saveConfig() {
  const payload = {
    rate_general_kwh:    +document.getElementById('rate_general').value,
    rate_ev_powerup_kwh: +document.getElementById('rate_ev_powerup').value,
    offpeak_start_hour:  +document.getElementById('offpeak_start').value,
    offpeak_end_hour:    +document.getElementById('offpeak_end').value,
    vehicles:            cfg.vehicles,
  };
  const r = await fetch('/api/config', {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  const st = document.getElementById('status');
  if (r.ok) {
    st.textContent = 'Saved.';
    setTimeout(() => st.textContent = '', 3000);
  } else {
    st.textContent = 'Error saving.';
    st.style.color = '#f44';
  }
}

load();
</script>
</body>
</html>"""


@app.route("/settings")
def settings():
    return Response(SETTINGS_HTML, mimetype="text/html")


@app.route("/")
def dashboard():
    return Response(DASHBOARD_HTML, mimetype="text/html")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Wall Connector server")
    p.add_argument("--port",   type=int, default=8090)
    p.add_argument("--host",   default="0.0.0.0", help="Bind address")
    p.add_argument("--wc-ip",  dest="wc_ip", help="Wall Connector IP (overrides config.json)")
    args = p.parse_args()

    # Load config.json then apply CLI overrides
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    elif not args.wc_ip:
        print(f"ERROR: config.json not found and --wc-ip not set.\n"
              f"Copy config.example.json → config.json and set wc_ip.")
        sys.exit(1)

    global WC_IP
    WC_IP = args.wc_ip or cfg.get("wc_ip", "")
    if not WC_IP:
        print("ERROR: wc_ip not set. Add it to config.json or use --wc-ip.")
        sys.exit(1)

    # Populate live CONFIG from file (handle old vehicles: ["A","B"] format)
    raw_vehicles = cfg.get("vehicles", CONFIG["vehicles"])
    if raw_vehicles and isinstance(raw_vehicles[0], str):
        raw_vehicles = [{"name": n, "max_power_w": 9999, "ev_powerup": False}
                        for n in raw_vehicles]
    CONFIG.update({
        "rate_general_kwh":    cfg.get("rate_general_kwh",    CONFIG["rate_general_kwh"]),
        "rate_ev_powerup_kwh": cfg.get("rate_ev_powerup_kwh", CONFIG["rate_ev_powerup_kwh"]),
        "offpeak_start_hour":  cfg.get("offpeak_start_hour",  CONFIG["offpeak_start_hour"]),
        "offpeak_end_hour":    cfg.get("offpeak_end_hour",    CONFIG["offpeak_end_hour"]),
        "vehicles":            raw_vehicles,
    })

    init_db()

    poll_thread = Thread(target=poller, daemon=True)
    poll_thread.start()

    def shutdown(sig, _frame):
        print("\nShutting down.")
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(f"Dashboard: http://localhost:{args.port}/")
    print(f"API:       http://localhost:{args.port}/api/status")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
