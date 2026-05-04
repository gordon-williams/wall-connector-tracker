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

# Defaults — override via config.json or CLI args
WC_IP             = ""      # required: set in config.json ("wc_ip") or --wc-ip
POLL_S            = 30
DB_PATH           = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wc_sessions.db")
CONFIG_PATH       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

RATE_GENERAL      = 0.30    # $/kWh general/peak — set in config.json ("rate_general_kwh")
RATE_EV_POWERUP   = 0.08    # $/kWh EV off-peak plan rate ("rate_ev_powerup_kwh")
OFFPEAK_START_H   = 22      # off-peak window start hour local time ("offpeak_start_hour")
OFFPEAK_END_H     = 7       # off-peak window end hour local time   ("offpeak_end_hour")

# Vehicle auto-detection by average charge power (W)
# Below threshold → vehicles[1], above → vehicles[0]
POWER_THRESHOLD_W = 8750    # configurable: "vehicle_power_threshold_w"
AUTO_TAG_AFTER_S  = 120     # wait N seconds before auto-tagging

# Vehicle names [high-power, low-power] — configurable: "vehicles": ["Tesla", "Shark"]
VEHICLE_HIGH      = "Tesla"
VEHICLE_LOW       = "Shark"

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


def is_offpeak(dt_local: datetime) -> bool:
    h = dt_local.hour
    return h >= OFFPEAK_START_H or h < OFFPEAK_END_H


def tesla_rate(start_time_iso: str) -> float:
    try:
        dt = datetime.fromisoformat(start_time_iso).astimezone()
        return RATE_EV_POWERUP if is_offpeak(dt) else RATE_GENERAL
    except Exception:
        return RATE_GENERAL


def fmt_duration(s):
    if not s:
        return None
    h, rem = divmod(int(s), 3600)
    m = rem // 60
    return f"{h}h {m:02d}m" if h else f"{m}m"


def session_cost(row: dict) -> float:
    wh   = row.get("energy_wh") or 0
    rate = row.get("rate_kwh") or RATE_GENERAL
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

                # Auto-tag vehicle once we have 2+ minutes of data
                if not poller_state["auto_tagged"] and duration >= AUTO_TAG_AFTER_S and poller_state["session_energy"] > 0:
                    avg_w   = poller_state["session_energy"] / (duration / 3600)
                    vehicle = VEHICLE_HIGH if avg_w > POWER_THRESHOLD_W else VEHICLE_LOW
                    start   = db_one("SELECT start_time FROM sessions WHERE id=?", (session_id,))
                    rate    = tesla_rate(start["start_time"]) if vehicle == "Tesla" else RATE_GENERAL
                    db_exec(
                        "UPDATE sessions SET vehicle=?, auto_tagged=1, rate_kwh=? WHERE id=?",
                        (vehicle, rate, session_id)
                    )
                    poller_state["auto_tagged"] = True
                    rate_str = f"${rate:.4f}/kWh"
                    print(f"[{now_utc.strftime('%H:%M:%S')}] Session {session_id} → {vehicle}  {avg_w:.0f}W  {rate_str}")

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

    lt = fetch_json(f"http://{WC_IP}/api/1/lifetime")

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
        if vehicle == VEHICLE_HIGH:
            rate = tesla_rate(row["start_time"])
        else:
            rate = RATE_GENERAL
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


@app.route("/api/config")
def api_config():
    return jsonify({
        "wc_ip":                WC_IP,
        "rate_general_kwh":     RATE_GENERAL,
        "rate_ev_powerup_kwh":  RATE_EV_POWERUP,
        "offpeak_start_hour":   OFFPEAK_START_H,
        "offpeak_end_hour":     OFFPEAK_END_H,
        "vehicle_high":         VEHICLE_HIGH,
        "vehicle_low":          VEHICLE_LOW,
        "vehicle_power_threshold_w": POWER_THRESHOLD_W,
    })


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
</style>
</head>
<body>
<header>
  <h1><span class="dot green" id="conn-dot"></span>Wall Connector</h1>
  <span id="poll-ts">–</span>
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
  </div>

  <!-- Toolbar -->
  <div class="toolbar">
    <button class="btn active" onclick="setDays(7)">7 days</button>
    <button class="btn" onclick="setDays(30)">30 days</button>
    <button class="btn" onclick="setDays(90)">90 days</button>
    <button class="btn" onclick="setDays(null)">All</button>
    <select class="filter" onchange="setVehicle(this.value)">
      <option value="">All vehicles</option>
      <option value="Tesla">Tesla</option>
      <option value="Shark">Shark</option>
      <option value="Unknown">Unknown</option>
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
let currentDays = 7;
let currentVehicle = '';

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
    } else {
      document.getElementById('s-cost').textContent = '–';
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
          <option ${vehicle==='Tesla'  ? 'selected' : ''}>Tesla</option>
          <option ${vehicle==='Shark'  ? 'selected' : ''}>Shark</option>
          <option ${vehicle==='Unknown'? 'selected' : ''}>Unknown</option>
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
      </td>`;
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

async function loadConfig() {
  try {
    const r = await fetch('/api/config');
    const c = await r.json();
    const note = document.getElementById('rate-note');
    if (note) {
      const s = c.offpeak_start_hour, e = c.offpeak_end_hour;
      const fmtH = h => h === 0 ? '12am' : h < 12 ? `${h}am` : h === 12 ? '12pm' : `${h-12}pm`;
      note.textContent =
        `~ = auto-tagged by power (threshold ${(c.vehicle_power_threshold_w/1000).toFixed(2)} kW)  •  ` +
        `${c.vehicle_high} off-peak (${fmtH(s)}–${fmtH(e)}) rate: $${c.rate_ev_powerup_kwh.toFixed(3)}/kWh  •  ` +
        `General rate: $${c.rate_general_kwh.toFixed(4)}/kWh`;
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
</body>
</html>"""


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

    global WC_IP, RATE_GENERAL, RATE_EV_POWERUP, OFFPEAK_START_H, OFFPEAK_END_H
    global POWER_THRESHOLD_W, VEHICLE_HIGH, VEHICLE_LOW
    WC_IP             = args.wc_ip or cfg.get("wc_ip", "")
    RATE_GENERAL      = cfg.get("rate_general_kwh",      RATE_GENERAL)
    RATE_EV_POWERUP   = cfg.get("rate_ev_powerup_kwh",   RATE_EV_POWERUP)
    OFFPEAK_START_H   = cfg.get("offpeak_start_hour",    OFFPEAK_START_H)
    OFFPEAK_END_H     = cfg.get("offpeak_end_hour",      OFFPEAK_END_H)
    POWER_THRESHOLD_W = cfg.get("vehicle_power_threshold_w", POWER_THRESHOLD_W)
    vehicles          = cfg.get("vehicles", [VEHICLE_HIGH, VEHICLE_LOW])
    VEHICLE_HIGH, VEHICLE_LOW = vehicles[0], vehicles[1]

    if not WC_IP:
        print("ERROR: wc_ip not set. Add it to config.json or use --wc-ip.")
        sys.exit(1)

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
