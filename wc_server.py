#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Gordon Williams — https://github.com/gordon-williams/wall-connector-tracker
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
    "offpeak_start_hour":  21,
    "offpeak_end_hour":    7,
    "vehicles": [
        {"name": "Tesla",   "max_power_w": 13000, "capacity_kwh": 82.0,  "ev_powerup": True},
        {"name": "Shark",   "max_power_w":  7000, "capacity_kwh": 26.5,  "ev_powerup": False},
        {"name": "Unknown", "max_power_w":  9999, "capacity_kwh":  0,    "ev_powerup": False},
    ],
}
CONFIG_LOCK = Lock()

app = Flask(__name__)

# ── Shared poller state (guarded by state_lock) ───────────────────────────────

state_lock   = Lock()
poller_state = {
    "session_id":           None,
    "was_charging":         False,
    "session_energy":       0.0,    # delta Wh charged this session (WC reading - baseline)
    "session_energy_baseline": 0.0, # WC's session_energy_wh at session start
    "session_start":        None,
    "auto_tagged":          False,
    "charge_duration_s":    0,      # sum of poll ticks where current_a > 0
    "last_vitals":          None,
    "last_poll_ts":         None,
    "poll_error":           False,
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
    # Add start_soc column to existing DBs that predate this field
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN start_soc REAL")
        conn.commit()
    except Exception:
        pass  # column already exists
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN end_soc REAL")
        conn.commit()
    except Exception:
        pass  # column already exists
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN energy_wh_baseline REAL DEFAULT 0")
        conn.commit()
    except Exception:
        pass  # column already exists
    conn.commit()
    conn.close()


def recalc_session_durations():
    """Recalculate duration_s for all completed sessions from samples (current_a > 0 ticks × POLL_S).
    Runs at startup — safe to repeat; sessions without samples are left unchanged."""
    sessions = db_query("SELECT id FROM sessions WHERE end_time IS NOT NULL")
    updated = 0
    for sess in sessions:
        sid = sess["id"]
        samples = db_query(
            "SELECT current_a FROM session_samples WHERE session_id=? ORDER BY ts", (sid,)
        )
        if not samples:
            continue
        charge_dur = sum(POLL_S for s in samples if (s.get("current_a") or 0) > 0)
        if charge_dur > 0:
            db_exec("UPDATE sessions SET duration_s=? WHERE id=?", (charge_dur, sid))
            updated += 1
    if updated:
        print(f"Recalculated charge duration for {updated} session(s)")


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

    # Resume any session left open by a previous server instance
    with state_lock:
        open_sess = db_one("SELECT * FROM sessions WHERE end_time IS NULL ORDER BY id DESC LIMIT 1")
        if open_sess:
            sid = open_sess["id"]
            try:
                start_dt = datetime.fromisoformat(open_sess["start_time"])
            except Exception:
                start_dt = None
            # Recalculate charge duration from existing samples (current_a > 0 ticks × POLL_S)
            samples = db_query(
                "SELECT current_a FROM session_samples WHERE session_id=? ORDER BY ts", (sid,)
            )
            charge_dur = sum(POLL_S for s in samples if (s.get("current_a") or 0) > 0)
            baseline = float(open_sess.get("energy_wh_baseline") or 0)
            poller_state.update(
                session_id=sid,
                session_energy=float(open_sess["energy_wh"] or 0),
                session_energy_baseline=baseline,
                session_start=start_dt,
                auto_tagged=bool(open_sess.get("auto_tagged")),
                was_charging=True,
                charge_duration_s=charge_dur,
            )
            print(f"Resumed unclosed session {sid} ({open_sess.get('vehicle','?')}) — {charge_dur}s charge time, baseline {baseline:.0f} Wh")

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

            # ── New session (or resume paused session) ─────────────────────
            if is_charging and not was_charging:
                # Check if this is a continuation of the previous session within 4 hours
                # (WC counter doesn't reset during scheduled-charging pauses)
                resumed = False
                prev = db_one("SELECT * FROM sessions WHERE end_time IS NOT NULL ORDER BY id DESC LIMIT 1")
                if prev:
                    try:
                        prev_end = datetime.fromisoformat(prev["end_time"])
                        if prev_end.tzinfo is None:
                            prev_end = prev_end.replace(tzinfo=timezone.utc)
                        gap_s = (now_utc - prev_end).total_seconds()
                        prev_total_wh = (prev.get("energy_wh_baseline") or 0) + (prev.get("energy_wh") or 0)
                        if gap_s <= 7200 and energy_wh >= prev_total_wh * 0.9:
                            # Same plug event — reopen previous session
                            db_exec("UPDATE sessions SET end_time=NULL WHERE id=?", (prev["id"],))
                            samps = db_query(
                                "SELECT current_a FROM session_samples WHERE session_id=? ORDER BY ts",
                                (prev["id"],)
                            )
                            charge_dur = sum(POLL_S for s in samps if (s.get("current_a") or 0) > 0)
                            try:
                                start_dt = datetime.fromisoformat(prev["start_time"])
                            except Exception:
                                start_dt = now_utc
                            poller_state.update(
                                session_id=prev["id"],
                                session_energy=float(prev.get("energy_wh") or 0),
                                session_energy_baseline=float(prev.get("energy_wh_baseline") or 0),
                                session_start=start_dt,
                                auto_tagged=bool(prev.get("auto_tagged")),
                                charge_duration_s=charge_dur,
                            )
                            print(f"[{now_utc.strftime('%H:%M:%S')}] Session {prev['id']} resumed after {int(gap_s/60)}m pause")
                            resumed = True
                    except Exception as exc:
                        print(f"Continuation check failed: {exc}")

                if not resumed:
                    # New plug event — record WC counter as baseline
                    sid = db_exec(
                        "INSERT INTO sessions (start_time, rate_kwh, energy_wh, energy_wh_baseline, vehicle) VALUES (?, ?, ?, ?, 'Unknown')",
                        (now_iso, CONFIG["rate_general_kwh"], 0.0, energy_wh)
                    )
                    poller_state.update(session_id=sid, session_energy=0.0,
                                        session_energy_baseline=energy_wh,
                                        session_start=now_utc, auto_tagged=False,
                                        charge_duration_s=0)
                    print(f"[{now_utc.strftime('%H:%M:%S')}] Session {sid} started (WC baseline {energy_wh:.0f} Wh)")

            # ── Daemon started mid-session ─────────────────────────────────
            elif is_charging and not session_id:
                sid = db_exec(
                    "INSERT INTO sessions (start_time, rate_kwh, energy_wh, energy_wh_baseline, vehicle) VALUES (?, ?, ?, ?, 'Unknown')",
                    (now_iso, CONFIG["rate_general_kwh"], 0.0, energy_wh)
                )
                poller_state.update(session_id=sid, session_energy=0.0,
                                    session_energy_baseline=energy_wh,
                                    session_start=now_utc, auto_tagged=False,
                                    charge_duration_s=0)
                print(f"[{now_utc.strftime('%H:%M:%S')}] Picked up in-progress session {sid} (WC baseline {energy_wh:.0f} Wh)")

            # ── Session in progress: update ────────────────────────────────
            elif is_charging and session_id:
                if energy_wh > 0:
                    poller_state["session_energy"] = energy_wh

                # Accumulate only ticks where current is actually flowing
                current_a = float(v.get("vehicle_current_a") or 0)
                if current_a > 0:
                    poller_state["charge_duration_s"] += POLL_S
                duration = poller_state["charge_duration_s"]

                # Energy = delta from baseline (WC counter never resets between sessions)
                if energy_wh > 0:
                    baseline = poller_state["session_energy_baseline"]
                    poller_state["session_energy"] = max(0.0, energy_wh - baseline)

                db_exec(
                    "UPDATE sessions SET end_time=?, duration_s=?, energy_wh=? WHERE id=?",
                    (now_iso, duration, poller_state["session_energy"], session_id)
                )
                db_exec(
                    "INSERT INTO session_samples (session_id, ts, energy_wh, current_a, grid_v) VALUES (?,?,?,?,?)",
                    (session_id, now_iso, energy_wh, v.get("vehicle_current_a"), v.get("grid_v"))
                )

                # Auto-tag vehicle once we have 2+ minutes of charge data
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
                duration     = poller_state["charge_duration_s"]
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
                                    session_energy_baseline=0.0,
                                    session_start=None, auto_tagged=False,
                                    charge_duration_s=0)

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

    if "start_soc" in body:
        updates.append("start_soc=?")
        params.append(float(body["start_soc"]) if body["start_soc"] is not None else None)

    if "end_soc" in body:
        updates.append("end_soc=?")
        params.append(float(body["end_soc"]) if body["end_soc"] is not None else None)

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


@app.route("/api/sessions/recent-per-vehicle")
def api_recent_per_vehicle():
    rows = db_query("""
        SELECT s.* FROM sessions s
        INNER JOIN (
            SELECT vehicle, MAX(id) AS max_id
            FROM sessions
            WHERE end_time IS NOT NULL AND vehicle NOT IN ('Unknown','')
            GROUP BY vehicle
        ) latest ON s.id = latest.max_id
        ORDER BY s.id DESC
    """)
    result = []
    for r in rows:
        d = dict(r)
        d["cost"] = session_cost(r)
        result.append(d)
    return jsonify(result)


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
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wall Connector</title>
<script>(function(){const t=localStorage.getItem('wc-theme');if(t)document.documentElement.dataset.theme=t})();</script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/html2canvas@1/dist/html2canvas.min.js"></script>
<style>
:root{
  --bg:#161616;--bg2:#1e1e1e;--bg3:#252525;
  --border:#2d2d2d;--border2:#383838;
  --text:#f0f0f0;--text2:#aaa;--label:#777;
  --accent:#00c853;
  --blue:#64b5f6;--blue-bg:#1a3a5c;--blue-bd:#1e5a9c;
  --grid-line:#2a2a2a;
}
[data-theme=light]{
  --bg:#f0f2f5;--bg2:#fff;--bg3:#f5f5f5;
  --border:#e0e0e0;--border2:#d0d0d0;
  --text:#111;--text2:#555;--label:#888;
  --blue:#1565c0;--blue-bg:#dbeafe;--blue-bd:#93c5fd;
  --grid-line:#e8e8e8;
}
@media(prefers-color-scheme:light){:root:not([data-theme=dark]){
  --bg:#f0f2f5;--bg2:#fff;--bg3:#f5f5f5;
  --border:#e0e0e0;--border2:#d0d0d0;
  --text:#111;--text2:#555;--label:#888;
  --blue:#1565c0;--blue-bg:#dbeafe;--blue-bd:#93c5fd;
  --grid-line:#e8e8e8;
}}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:14px}
header{padding:12px 20px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;gap:12px}
header h1{font-size:15px;font-weight:600;color:var(--text);white-space:nowrap;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis}
.header-right{display:flex;gap:10px;align-items:center;flex-shrink:0}
#poll-ts{font-size:11px;color:var(--label)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.dot.green{background:#00c853}.dot.red{background:#f44336}.dot.amber{background:#ff9800}
.container{max-width:1100px;margin:0 auto;padding:16px 20px}

/* Pill buttons */
.pill{background:var(--bg3);border:1px solid var(--border2);color:var(--text2);padding:5px 13px;border-radius:20px;cursor:pointer;font-size:12px;font-weight:500;transition:all .15s;white-space:nowrap;line-height:1.5}
.pill:hover{border-color:var(--blue-bd);color:var(--blue)}
.pill.active{background:var(--blue-bg);border-color:var(--blue-bd);color:var(--blue)}
.btn-group{display:flex;gap:4px;flex-wrap:wrap}
.icon-btn{background:none;border:1px solid var(--border2);color:var(--label);width:30px;height:30px;border-radius:6px;cursor:pointer;font-size:14px;display:flex;align-items:center;justify-content:center;transition:all .15s}
.icon-btn:hover{border-color:var(--border);color:var(--text2)}
.nav-link{font-size:12px;color:var(--label);text-decoration:none;padding:5px 11px;border:1px solid var(--border2);border-radius:5px;transition:all .15s}
.nav-link:hover{color:var(--text);border-color:var(--border)}

/* Status cards */
.status-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:16px}
.stat{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:12px 14px;min-width:0;overflow:visible}
.stat-label{font-size:11px;color:var(--label);text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px}
.stat-value{font-size:17px;font-weight:600;color:var(--text);white-space:nowrap;overflow:visible}
.stat-value.green{color:#00c853}
.stat-value.dim{color:var(--text2);font-size:13px;white-space:nowrap;overflow:visible}
.badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600}
.badge.charging{background:#1a3d25;color:#00c853}
.badge.idle{background:var(--bg3);color:var(--label)}
.badge.tesla{background:#1a3a5c;color:#64b5f6}
.badge.shark{background:#3b2f00;color:#ffd54f}
.badge.leaf{background:#1a3a1e;color:#81c784}

/* Section card */
.section-card{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:14px 18px;margin-bottom:16px}
.section-header{display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap}
.section-title{font-size:12px;font-weight:600;color:var(--text2);text-transform:uppercase;letter-spacing:.05em}
.stats-controls{display:flex;align-items:center;gap:8px;flex-wrap:wrap;flex:1}

/* Live section */
.live-section{display:none;background:var(--bg2);border:1px solid #1e3a1e;border-radius:8px;padding:14px 18px;margin-bottom:16px}
.live-section.completed{border-color:var(--border)}
.live-section.completed .live-title{color:var(--text2)}
.live-header{display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap}
.live-title{font-size:12px;font-weight:600;color:#00c853}
.live-sub{font-size:11px;color:var(--label)}
.live-body{display:flex;gap:16px;align-items:flex-start}
.live-chart-wrap{flex:1;height:190px;position:relative;min-width:0}

/* SOC gauge */
.live-soc{width:160px;flex-shrink:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;padding-top:4px}
.soc-svg{width:148px;height:auto}
.soc-bg{fill:none;stroke:var(--border2);stroke-width:11;stroke-linecap:round}
.soc-val{fill:none;stroke-width:11;stroke-linecap:round}
.soc-num{font-size:22px;font-weight:700;fill:var(--text);font-family:-apple-system,BlinkMacSystemFont,sans-serif}
.soc-sub{font-size:11px;fill:var(--label);font-family:-apple-system,BlinkMacSystemFont,sans-serif}
.soc-row{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text2)}
.soc-inp{width:54px;background:var(--bg3);border:1px solid var(--border2);color:var(--text);padding:4px 6px;border-radius:4px;font-size:13px;text-align:center}
.soc-inp:focus{outline:none;border-color:var(--blue-bd)}
.soc-note{font-size:10px;color:var(--label);text-align:center;line-height:1.4}

/* Stats chart */
.stats-chart-wrap{height:240px;position:relative}

/* Recent per vehicle */
.recent-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin-bottom:16px}
.recent-card{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:12px 14px;display:flex;flex-direction:column;gap:4px}
.recent-card-top{display:flex;align-items:center;gap:8px;margin-bottom:4px}
.recent-meta{font-size:11px;color:var(--label)}
.recent-kwh{font-size:18px;font-weight:600;color:var(--text)}
.recent-cost{font-size:12px;color:var(--text2)}

/* Session table */
td.td-date,td.td-time,td.td-dur{white-space:nowrap}
.toolbar{display:flex;gap:6px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
.spacer{flex:1}
.table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;border-radius:6px}
table{width:100%;border-collapse:collapse;min-width:580px}
th{text-align:left;padding:7px 10px;font-size:11px;color:var(--label);text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:7px 10px;border-bottom:1px solid var(--border);vertical-align:middle}
tr:hover td{background:var(--bg3)}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.vehicle-select{background:var(--bg3);border:1px solid var(--border2);color:var(--text2);padding:3px 6px;border-radius:4px;font-size:12px;cursor:pointer;min-width:80px}
.vehicle-select.tesla{background:#1a3a5c;border-color:#1e5a9c;color:#64b5f6}
.vehicle-select.shark{background:#3b2f00;border-color:#6b5200;color:#ffd54f}
.vehicle-select.leaf{background:#1a3a1e;border-color:#2a5a2e;color:#81c784}
.note-input{background:var(--bg3);border:1px solid var(--border2);color:var(--text);padding:3px 6px;border-radius:4px;font-size:12px;width:140px}
.auto-tag{font-size:10px;color:var(--label);margin-left:4px}
.total-row td{border-top:1px solid var(--border2);border-bottom:none;font-weight:600;color:var(--text)}
select.filter{background:var(--bg3);border:1px solid var(--border2);color:var(--text2);padding:5px 8px;border-radius:5px;font-size:12px;cursor:pointer}
.soc-end-inp{width:44px;background:var(--bg3);border:1px solid var(--border2);color:var(--text);padding:2px 4px;border-radius:3px;font-size:12px;text-align:center}
.soc-end-inp::-webkit-inner-spin-button,.soc-end-inp::-webkit-outer-spin-button{-webkit-appearance:none;margin:0}
.soc-end-inp{-moz-appearance:textfield}
.soc-end-inp:focus{outline:none;border-color:var(--blue-bd)}
.trend-btn{background:none;border:1px solid var(--border2);color:var(--label);padding:2px 7px;border-radius:3px;cursor:pointer;font-size:11px}
.trend-btn:hover{border-color:var(--border);color:var(--text2)}
#session-count{font-size:11px;color:var(--label)}

/* Summary */
.summary-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-top:16px}
.sum-card{background:var(--bg2);border:1px solid var(--border);border-radius:7px;padding:12px 14px}
.sum-label{font-size:11px;color:var(--label);margin-bottom:4px}
.sum-val{font-size:15px;font-weight:600;color:var(--text)}
.offpeak-note{font-size:11px;color:var(--label);margin-top:10px;text-align:center}

/* Chart modal */
#chart-modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.82);z-index:100;align-items:center;justify-content:center}
.modal-box{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:20px;width:min(1100px,92vw);position:relative;max-height:90vh;overflow-y:auto}
.modal-close{position:absolute;top:10px;right:14px;background:none;border:none;color:var(--label);font-size:18px;cursor:pointer}
.modal-close:hover{color:var(--text)}
.modal-title{color:var(--text);font-size:13px;font-weight:600;margin-bottom:4px}
.modal-sub{font-size:11px;color:var(--label);margin-bottom:14px}
.chart-wrap{height:420px;position:relative}
.modal-toggle{display:flex;gap:6px;margin-bottom:12px}

@media(max-width:600px){.desktop-only{display:none!important}}
@media(min-width:601px){.mobile-only{display:none!important}}
@media(max-width:600px){
  header{padding:10px 14px}
  .container{padding:12px 14px}
  .status-grid{grid-template-columns:repeat(2,1fr);gap:8px}
  .stats-chart-wrap{height:180px}
  .chart-wrap{height:220px}
  .note-input{width:100px}
  .summary-grid{grid-template-columns:repeat(2,1fr)}
  .stat-value{font-size:15px}
  .live-body{flex-direction:column}
  .live-chart-wrap{width:100%;height:160px}
  .live-soc{width:100%;flex-direction:row;gap:24px;padding-top:0;justify-content:center}
  .soc-svg{width:110px}

  /* Header */
  header{padding:10px 14px}
  header h1{font-size:13px}
  #poll-ts{display:none}
  #s-firmware{display:block;margin-left:0;margin-top:2px}

  /* Table: hide low-priority columns, keep native table layout */
  table{min-width:0}
  td.td-id,th.th-id,
  td.td-time,th.th-time,
  td.td-avgw,th.th-avgw,
  td.td-rate,th.th-rate,
  td.td-notes,th.th-notes,
  td.td-trend,th.th-trend{display:none}
  th,td{padding:5px 7px;font-size:12px}
}
</style>
</head>
<body>
<header>
  <h1><span class="dot green" id="conn-dot"></span>Tesla Wall Connector<span id="s-firmware" style="font-size:11px;font-weight:400;color:var(--label);margin-left:14px"></span></h1>
  <div class="header-right">
    <span id="poll-ts">–</span>
    <button class="icon-btn" id="theme-btn" onclick="toggleTheme()" title="Toggle theme">◑</button>
    <a class="nav-link" href="/settings">Settings</a>
  </div>
</header>

<div class="container">

  <!-- Status cards -->
  <div class="status-grid">
    <div class="stat"><div class="stat-label">Grid</div><div class="stat-value dim" id="s-grid">–</div></div>
    <div class="stat"><div class="stat-label">State</div><div class="stat-value" id="s-state">–</div></div>
    <div class="stat"><div class="stat-label">Energy</div><div class="stat-value green" id="s-energy">–</div></div>
    <div class="stat"><div class="stat-label">Duration</div><div class="stat-value" id="s-dur">–</div></div>
    <div class="stat"><div class="stat-label">Avg power</div><div class="stat-value" id="s-power">–</div></div>
    <div class="stat"><div class="stat-label">Est. cost</div><div class="stat-value" id="s-cost">–</div></div>
    <div class="stat"><div class="stat-label">WiFi</div><div class="stat-value dim" id="s-wifi">–</div></div>
  </div>

  <!-- Live charge -->
  <div class="live-section" id="live-section">
    <div class="live-header">
      <span class="dot green"></span>
      <span class="live-title" id="live-title">Charging now</span>
      <span class="live-sub" id="live-sub"></span>
      <div style="flex:1"></div>
      <div class="btn-group">
        <button class="pill active" id="live-btn-energy" onclick="switchLiveAxis('energy')">vs Energy</button>
        <button class="pill" id="live-btn-time" onclick="switchLiveAxis('time')">vs Time</button>
      </div>
    </div>
    <div class="live-body">
      <div class="live-chart-wrap"><canvas id="live-chart"></canvas></div>
      <!-- SOC gauge -->
      <div class="live-soc">
        <svg class="soc-svg" viewBox="0 0 120 84">
          <path class="soc-bg" d="M8,74 A52,52,0,0,1,112,74"/>
          <path class="soc-val" id="soc-arc-base" d="" stroke="#1e5a9c"/>
          <path class="soc-val" id="soc-arc" d="" stroke="#00c853"/>
          <text id="soc-pct" x="60" y="66" text-anchor="middle" class="soc-num">–</text>
          <text x="60" y="80" text-anchor="middle" class="soc-sub">SOC</text>
        </svg>
        <div class="soc-row">
          <span style="font-size:11px;color:var(--label)">Start&nbsp;%</span>
          <input id="soc-start" class="soc-inp" type="number" min="0" max="100" placeholder="0" oninput="updateSOC()">
        </div>
        <div id="soc-note" class="soc-note"></div>
        <div id="soc-pred" class="soc-note" style="color:var(--text2)"></div>
      </div>
    </div>
  </div>

  <!-- Stats chart -->
  <div class="section-card">
    <div class="section-header">
      <span class="section-title">Charging History</span>
      <div class="stats-controls">
        <div class="btn-group desktop-only">
          <button class="pill active" id="stats-month" onclick="setStatsRange('month')">Month</button>
          <button class="pill" id="stats-year"  onclick="setStatsRange('year')">Year</button>
          <button class="pill" id="stats-all"   onclick="setStatsRange('all')">All Time</button>
        </div>
        <div class="btn-group desktop-only" id="stats-vehicle-btns">
          <button class="pill active" id="statsv-all" onclick="setStatsVehicle('')">All</button>
        </div>
        <div class="mobile-only" style="display:flex;gap:6px;flex:1">
          <select class="filter" style="flex:1" id="stats-range-sel" onchange="setStatsRange(this.value)">
            <option value="month">Month</option>
            <option value="year">Year</option>
            <option value="all">All Time</option>
          </select>
          <select class="filter" style="flex:1" id="stats-vehicle-sel" onchange="setStatsVehicle(this.value)">
            <option value="">All vehicles</option>
          </select>
        </div>
      </div>
    </div>
    <div id="stats-nav" style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
      <button class="icon-btn" onclick="statsNavPrev()">‹</button>
      <span id="stats-nav-label" style="font-size:13px;color:var(--text2);min-width:100px;text-align:center"></span>
      <button class="icon-btn" onclick="statsNavNext()">›</button>
      <button class="pill" onclick="saveStatsImage()" style="margin-left:auto;font-size:12px">⬇ Save</button>
    </div>
    <div class="stats-chart-wrap"><canvas id="stats-chart"></canvas></div>
  </div>

  <!-- Recent per vehicle -->
  <div id="recent-per-vehicle" class="recent-grid"></div>

  <!-- Toolbar -->
  <div class="toolbar">
    <div class="btn-group desktop-only">
      <button class="pill active" id="days-7"   onclick="setDays(7,this)">7 days</button>
      <button class="pill" id="days-30" onclick="setDays(30,this)">30 days</button>
      <button class="pill" id="days-90" onclick="setDays(90,this)">90 days</button>
      <button class="pill" id="days-all" onclick="setDays(null,this)">All</button>
    </div>
    <select class="filter mobile-only" id="days-sel" onchange="setDays(this.value?+this.value:null,null)">
      <option value="7">Last 7 days</option>
      <option value="30">Last 30 days</option>
      <option value="90">Last 90 days</option>
      <option value="">All time</option>
    </select>
    <select class="filter" id="vehicle-filter" onchange="setVehicle(this.value)">
      <option value="">All vehicles</option>
    </select>
    <div class="spacer"></div>
    <button class="pill" onclick="exportCSV()">⬇ Export CSV</button>
    <span id="session-count">–</span>
  </div>

  <!-- Table -->
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th class="th-id">#</th><th class="th-date">Date</th><th class="th-time">Start</th><th class="th-vehicle">Vehicle</th>
          <th class="th-kwh num">kWh</th><th class="th-avgw num">Avg kW</th>
          <th class="th-dur num">Duration</th><th class="th-rate num">Rate</th>
          <th class="th-cost num">Cost</th><th class="th-soc">SOC</th><th class="th-notes">Notes</th><th class="th-trend"></th>
        </tr>
      </thead>
      <tbody id="sessions-tbody"></tbody>
      <tfoot id="sessions-tfoot"></tfoot>
    </table>
  </div>

  <!-- Summary -->
  <div class="summary-grid" id="summary-grid"></div>
  <p class="offpeak-note" id="rate-note"></p>

</div>

<!-- Toast notification -->
<div id="toast" style="display:none;position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:rgba(0,0,0,.82);color:#fff;padding:8px 20px;border-radius:20px;font-size:13px;z-index:300;pointer-events:none;white-space:nowrap"></div>

<!-- Modal before <script> so addEventListener can find it -->
<div id="chart-modal">
  <div class="modal-box">
    <button class="modal-close" onclick="closeChart()">✕</button>
    <div class="modal-title" id="chart-title"></div>
    <div class="modal-sub" id="chart-sub"></div>
    <div class="modal-toggle">
      <button class="pill active" id="btn-energy" onclick="switchAxis('energy')">Power vs Energy</button>
      <button class="pill" id="btn-time" onclick="switchAxis('time')">Power vs Time</button>
      <button id="btn-save-chart" class="pill" onclick="saveChartImage()" style="margin-left:auto">⬇ Save</button>
    </div>
    <div class="chart-wrap"><canvas id="trend-chart"></canvas></div>
    <p style="font-size:11px;color:var(--label);margin-top:10px;text-align:center">
      <span id="chart-footnote">Calculated from 30-second energy samples · X-axis approximates SOC progression</span>
    </p>
  </div>
</div>

<script>
// ── Theme ─────────────────────────────────────────────────────────────────────
function isLight() {
  const t = document.documentElement.dataset.theme;
  return t==='light' || (!t && window.matchMedia('(prefers-color-scheme: light)').matches);
}
function toggleTheme() {
  document.documentElement.dataset.theme = isLight() ? 'dark' : 'light';
  localStorage.setItem('wc-theme', document.documentElement.dataset.theme);
  updateThemeBtn(); redrawCharts();
}
function updateThemeBtn() {
  const btn = document.getElementById('theme-btn');
  if (btn) btn.textContent = isLight() ? '☾' : '☀';
}
function redrawCharts() {
  if (statsChart)    { const r=statsRange,v=statsVehicle; statsChart.destroy(); statsChart=null; _renderStats(); }
  if (liveChart)     { const s=_liveSamplesCache; if(s) renderLiveChart(s); }
  if (chartInstance) renderChart();
}
updateThemeBtn();

function cc() {
  const l = isLight();
  return {
    grid:    l ? '#e4e4e4' : '#2a2a2a',
    tick:    l ? '#888'    : '#777',
    tip:     { bg:l?'#fff':'#252525', border:l?'#ddd':'#333', title:l?'#333':'#ddd', body:l?'#555':'#aaa' }
  };
}
function vehicleColor(name) {
  const n=(name||'').toLowerCase(), l=isLight();
  if (n.includes('tesla'))                       return l ? '#1565c0' : '#1e5a9c';
  if (n.includes('shark')||n.includes('byd'))   return l ? '#b45309' : '#7a5a00';
  if (n.includes('leaf'))                        return l ? '#15803d' : '#1e5a2e';
  return l ? '#9ca3af' : '#484848';
}

// ── State ─────────────────────────────────────────────────────────────────────
let currentDays      = 7;
let currentVehicle   = '';
let vehicleNames     = ['Unknown'];
let vehicleCapacities = {};       // name → capacity_kwh
let liveSessionId    = null;
let liveAxis         = 'energy';
let liveChart        = null;
let _liveSamplesCache = null;
let liveVehicleName  = '';
let liveEnergyWh     = 0;
let liveDuration_s   = 0;
let statsRange       = 'month';
let statsNavYear     = new Date().getFullYear();
let statsNavMonth    = new Date().getMonth();
let statsVehicle     = '';
let statsChart       = null;
let _statsAllData    = [];
let chartInstance    = null;
let _socSaveTimer    = null;
let vehicleEfficiency = {};   // name → float (0.0–1.0)
let chartSamples     = [];
let chartAxis        = 'energy';

function fmtDur(s) {
  const h=Math.floor(s/3600), m=Math.floor((s%3600)/60);
  return h ? `${h}h ${String(m).padStart(2,'0')}m` : `${m}m`;
}

// ── SOC gauge ─────────────────────────────────────────────────────────────────
function socArcPath(fromPct, toPct) {
  const p0 = Math.max(0, Math.min(1, fromPct/100));
  const p1 = Math.max(0, Math.min(1, toPct/100));
  if (p1 <= p0) return '';
  const a0 = Math.PI*(1-p0), a1 = Math.PI*(1-p1);
  const x0=60+52*Math.cos(a0), y0=74-52*Math.sin(a0);
  const x1=60+52*Math.cos(a1), y1=74-52*Math.sin(a1);
  return `M${x0.toFixed(1)},${y0.toFixed(1)} A52,52,0,0,1,${x1.toFixed(1)},${y1.toFixed(1)}`;
}
function socColor(pct) {
  return pct<20?'#f44336':pct<50?'#ff9800':pct<80?'#00c853':'#64b5f6';
}
function updateSOC() {
  const cap    = vehicleCapacities[liveVehicleName] || 0;
  const arcEl  = document.getElementById('soc-arc');
  const pctEl  = document.getElementById('soc-pct');
  const noteEl = document.getElementById('soc-note');
  if (!arcEl) return;

  if (!cap) {
    noteEl.textContent = liveVehicleName && liveVehicleName!=='Unknown'
      ? 'Set capacity in Settings' : '';
    pctEl.textContent = '–';
    arcEl.setAttribute('d','');
    return;
  }
  noteEl.textContent = `${cap} kWh battery`;

  const startEl = document.getElementById('soc-start');
  const start   = parseFloat(startEl.value);
  if (isNaN(start)) { pctEl.textContent='–'; arcEl.setAttribute('d',''); return; }

  if (liveSessionId) {
    localStorage.setItem('wc-soc-'+liveSessionId, String(start));
    clearTimeout(_socSaveTimer);
    _socSaveTimer = setTimeout(() => {
      fetch(`/api/sessions/${liveSessionId}`, {
        method: 'PATCH', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({start_soc: start})
      });
    }, 800);
  }

  const calibration = vehicleEfficiency[liveVehicleName];
  const eta = calibration ? calibration.eta : 1.0;
  const soc = Math.min(100, start + (liveEnergyWh/1000*eta/cap)*100);
  pctEl.textContent = soc.toFixed(1)+'%';

  // Two-tone arc: blue = pre-existing, green = charged this session
  const baseArcEl = document.getElementById('soc-arc-base');
  if (baseArcEl) baseArcEl.setAttribute('d', start > 0 ? socArcPath(0, start) : '');
  arcEl.setAttribute('d', socArcPath(start, soc));
  arcEl.setAttribute('stroke', socColor(soc));

  // Charge time prediction (only when live and making progress)
  const predEl = document.getElementById('soc-pred');
  if (predEl) {
    const pctAdded = soc - start;
    if (pctAdded > 0.5 && liveDuration_s > 60 && soc < 100) {
      const ratePerS = pctAdded / liveDuration_s;
      const secsLeft = Math.round((100 - soc) / ratePerS);
      const finish = new Date(Date.now() + secsLeft*1000);
      const hm = finish.toLocaleTimeString('en-AU',{hour:'2-digit',minute:'2-digit'});
      predEl.textContent = `~${fmtDur(secsLeft)} to 100% · ${hm}`;
    } else {
      predEl.textContent = '';
    }
  }
}

// ── Status ────────────────────────────────────────────────────────────────────
async function loadStatus() {
  try {
    const d  = await fetch('/api/status').then(r=>r.json());
    const v  = d.vitals || {}, cs = d.current_session;

    document.getElementById('conn-dot').className = 'dot '+(d.ok?'green':'red');
    document.getElementById('poll-ts').textContent = d.last_poll
      ? 'Updated '+new Date(d.last_poll).toLocaleTimeString() : '–';

    document.getElementById('s-grid').textContent =
      v.grid_v ? `${v.grid_v.toFixed(1)} V / ${v.grid_hz?v.grid_hz.toFixed(2):'–'} Hz` : '–';

    if (v.contactor_closed)
      document.getElementById('s-state').innerHTML='<span class="badge charging">Charging</span>';
    else if (v.vehicle_connected)
      document.getElementById('s-state').innerHTML='<span class="badge idle">Connected</span>';
    else
      document.getElementById('s-state').innerHTML='<span class="badge idle">Idle</span>';

    const wh  = cs?cs.energy_wh:(v.session_energy_wh||0);
    const dur  = cs?cs.duration_s:(v.session_s||0);
    document.getElementById('s-energy').textContent = wh  ? (wh/1000).toFixed(2)+' kWh' : '–';
    document.getElementById('s-dur').textContent    = dur ? fmtDur(dur) : '–';
    const avgW = (wh&&dur>10) ? (wh/(dur/3600)) : null;
    document.getElementById('s-power').textContent  = avgW ? (avgW/1000).toFixed(2)+' kW' : '–';

    if (cs) {
      document.getElementById('s-cost').textContent='$'+cs.cost.toFixed(2);
      const lsEl = document.getElementById('live-section');
      lsEl.style.display='block';
      lsEl.classList.remove('completed');
      document.getElementById('live-title').textContent=`Session #${cs.id} — ${cs.vehicle||'detecting…'}`;
      document.getElementById('live-sub').textContent=cs.energy_wh?(cs.energy_wh/1000).toFixed(2)+' kWh':'–';

      if (cs.id !== liveSessionId) {
        liveSessionId = cs.id;
        if (liveChart) { liveChart.destroy(); liveChart=null; }
        // Restore starting SOC: DB is authoritative, localStorage is fallback
        const startEl = document.getElementById('soc-start');
        if (cs.start_soc != null) {
          startEl.value = cs.start_soc;
        } else {
          startEl.value = localStorage.getItem('wc-soc-'+liveSessionId) || '';
        }
      }
      liveVehicleName  = cs.vehicle || '';
      liveEnergyWh     = cs.energy_wh || 0;
      liveDuration_s   = cs.duration_s || 0;
      updateSOC();
      loadLiveSamples(cs.id);
    } else {
      document.getElementById('s-cost').textContent='–';
      if (liveSessionId) {
        // Keep last session visible, marked as completed
        const lsEl = document.getElementById('live-section');
        lsEl.style.display='block';
        lsEl.classList.add('completed');
        document.getElementById('live-title').textContent=`Last session #${liveSessionId} — ${liveVehicleName||''}`;
        document.getElementById('live-sub').textContent=liveEnergyWh?(liveEnergyWh/1000).toFixed(2)+' kWh · completed':'completed';
        const predEl = document.getElementById('soc-pred');
        if (predEl) predEl.textContent='';
      } else {
        document.getElementById('live-section').style.display='none';
      }
    }

    const wifi=d.wifi||{}, wifiEl=document.getElementById('s-wifi');
    if (wifi.wifi_connected) {
      const rssi=wifi.wifi_rssi||0;
      const str=rssi>=-50?'Excellent':rssi>=-65?'Good':rssi>=-75?'Fair':'Weak';
      wifiEl.textContent=`${str} (${rssi} dBm)${wifi.internet?'':' · No internet'}`;
      wifiEl.title=wifi.wifi_ssid_decoded||'';
    } else { wifiEl.textContent='Disconnected'; }

    const ver=d.version||{}, fwEl=document.getElementById('s-firmware');
    if (ver.firmware_version) fwEl.textContent='Firmware '+ver.firmware_version.split('+')[0];
  } catch(e) {
    document.getElementById('conn-dot').className='dot red';
    console.error('loadStatus:',e);
  }
}

// ── Sessions ──────────────────────────────────────────────────────────────────
function setDays(d, btn) {
  currentDays = d;
  document.querySelectorAll('.toolbar .pill').forEach(b=>b.classList.remove('active'));
  if (btn) {
    btn.classList.add('active');
  } else {
    const el = document.getElementById(d ? 'days-'+d : 'days-all');
    if (el) el.classList.add('active');
  }
  const sel = document.getElementById('days-sel');
  if (sel) sel.value = d ?? '';
  loadSessions();
}
function setVehicle(v) { currentVehicle=v; loadSessions(); }

async function loadSessions() {
  let url='/api/sessions';
  const p=[];
  if (currentDays) p.push('days='+currentDays);
  if (currentVehicle) p.push('vehicle='+encodeURIComponent(currentVehicle));
  if (p.length) url+='?'+p.join('&');

  const rows=await fetch(url).then(r=>r.json());
  document.getElementById('session-count').textContent=rows.length===1?'1 session':rows.length+' sessions';

  const tbody=document.getElementById('sessions-tbody');
  tbody.innerHTML='';
  let totalWh=0,totalCost=0;

  for (const row of rows) {
    const dt=row.start_time?new Date(row.start_time):null;
    const dateStr=dt?dt.toLocaleDateString('en-AU'):'–';
    const timeStr=dt?dt.toLocaleTimeString('en-AU',{hour:'2-digit',minute:'2-digit'}):'–';
    const wh=row.energy_wh||0, dur=row.duration_s||0;
    const avgW=(wh&&dur>10)?(wh/(dur/3600)/1000):null;
    const vehicle=row.vehicle||'Unknown';
    const vn=vehicle.toLowerCase();
    const vClass=vn.includes('tesla')?'tesla':(vn.includes('shark')||vn.includes('byd'))?'shark':vn.includes('leaf')?'leaf':'';
    totalWh+=wh; totalCost+=row.cost||0;

    const tr=document.createElement('tr');
    tr.innerHTML=`
      <td class="td-id" style="color:var(--label);font-size:12px">${row.id}</td>
      <td class="td-date" data-label="Date">${dateStr}</td>
      <td class="td-time" data-label="Start">${timeStr}</td>
      <td class="td-vehicle">
        <select class="vehicle-select ${vClass}" onchange="patchSession(${row.id},'vehicle',this.value,this)">
          ${vehicleNames.map(n=>`<option ${vehicle===n?'selected':''}>${n}</option>`).join('')}
          ${vehicleNames.includes(vehicle)?'':`<option selected>${vehicle}</option>`}
        </select>
      </td>
      <td class="td-kwh num" data-label="kWh">${wh?(wh/1000).toFixed(2):'–'}</td>
      <td class="td-avgw num" data-label="Avg kW">${avgW!=null?avgW.toFixed(2):'–'}</td>
      <td class="td-dur num" data-label="Duration">${dur?fmtDur(dur):'–'}</td>
      <td class="td-rate num" data-label="Rate" style="font-size:11px;color:var(--label)">$${(row.rate_kwh||0).toFixed(4)}</td>
      <td class="td-cost num" data-label="Cost">$${(row.cost||0).toFixed(2)}</td>
      <td class="td-soc" data-label="SOC" style="white-space:nowrap"><input class="soc-end-inp" type="number" min="0" max="100" value="${row.start_soc??''}" placeholder="?" style="width:36px" onblur="patchSoc(${row.id},'start_soc',this.value)" onkeydown="if(event.key==='Enter')this.blur()"><span style="font-size:10px;color:var(--label);margin:0 2px">→</span><input class="soc-end-inp" type="number" min="0" max="100" value="${row.end_soc??''}" placeholder="?" style="width:36px" onblur="patchSoc(${row.id},'end_soc',this.value)" onkeydown="if(event.key==='Enter')this.blur()"></td>
      <td class="td-notes" data-label="Notes"><input class="note-input" value="${(row.notes||'').replace(/"/g,'&quot;')}"
            onblur="patchSession(${row.id},'notes',this.value,null)"
            onkeydown="if(event.key==='Enter')this.blur()"></td>
      <td class="td-trend"><button class="trend-btn" onclick="showChart(${row.id},'${vehicle}',${wh/1000})">Trend</button></td>`;
    tbody.appendChild(tr);
  }

  document.getElementById('sessions-tfoot').innerHTML=`<tr class="total-row">
    <td colspan="4">Total</td>
    <td class="num">${(totalWh/1000).toFixed(2)}</td>
    <td colspan="3"></td>
    <td class="num">$${totalCost.toFixed(2)}</td>
    <td colspan="3"></td></tr>`;
  loadSummary();
  loadRecentPerVehicle();
}

async function loadRecentPerVehicle() {
  const sessions = await fetch('/api/sessions/recent-per-vehicle').then(r=>r.json());
  const grid = document.getElementById('recent-per-vehicle');
  if (!grid) return;
  grid.innerHTML = '';
  for (const s of sessions) {
    const vn = (s.vehicle||'').toLowerCase();
    const vClass = vn.includes('tesla')?'tesla':(vn.includes('shark')||vn.includes('byd'))?'shark':vn.includes('leaf')?'leaf':'';
    const dt = s.start_time ? new Date(s.start_time) : null;
    const dateStr = dt ? dt.toLocaleDateString('en-AU',{weekday:'short',day:'numeric',month:'short'}) : '–';
    const kWh = s.energy_wh ? (s.energy_wh/1000).toFixed(2) : '–';
    const cost = s.cost ? '$'+s.cost.toFixed(2) : '–';
    const dur = s.duration_s ? fmtDur(s.duration_s) : '';
    const card = document.createElement('div');
    card.className = 'recent-card';
    card.innerHTML = `
      <div class="recent-card-top">
        <span class="badge ${vClass}">${s.vehicle||'Unknown'}</span>
        <span class="recent-meta" style="flex:1;text-align:right">#${s.id}</span>
      </div>
      <div class="recent-meta">${dateStr}${dur?' · '+dur:''}</div>
      <div class="recent-kwh">${kWh} kWh</div>
      <div style="display:flex;align-items:center;justify-content:space-between">
        <span class="recent-cost">${cost}</span>
        <button class="trend-btn" onclick="showChart(${s.id},'${s.vehicle}',${(s.energy_wh||0)/1000})">Trend</button>
      </div>`;
    grid.appendChild(card);
  }
}

async function loadSummary() {
  const data=await fetch('/api/summary').then(r=>r.json());
  const grid=document.getElementById('summary-grid');
  grid.innerHTML='';
  const t=data.totals||{};
  addCard(grid,'All time sessions',(t.sessions||0).toLocaleString(),'');
  addCard(grid,'All time energy',t.total_wh?(t.total_wh/1000).toFixed(1)+' kWh':'–','');
  addCard(grid,'All time cost',t.total_cost?'$'+t.total_cost.toFixed(2):'–','');
  for (const v of (data.by_vehicle||[])) {
    if (!v.vehicle||v.vehicle==='Unknown') continue;
    const vn=v.vehicle.toLowerCase();
    const cls=vn.includes('tesla')?'tesla':(vn.includes('shark')||vn.includes('byd'))?'shark':vn.includes('leaf')?'leaf':'';
    const eff = vehicleEfficiency[v.vehicle];
    const effStr = eff ? `${(eff.eta*100).toFixed(1)}% eff (${eff.count} cal.)` : '';
    addCard(grid,`<span class="badge ${cls}">${v.vehicle}</span>`,
      `${v.count} sessions · ${v.total_wh?(v.total_wh/1000).toFixed(1)+' kWh':'–'}`,
      [v.total_cost?'$'+v.total_cost.toFixed(2):'–', effStr].filter(Boolean).join(' · '));
  }
}

function addCard(grid,label,value,sub) {
  const div=document.createElement('div');
  div.className='sum-card';
  div.innerHTML=`<div class="sum-label">${label}</div><div class="sum-val">${value}</div>
    ${sub?`<div style="font-size:12px;color:var(--text2);margin-top:4px">${sub}</div>`:''}`;
  grid.appendChild(div);
}

async function patchSession(id,field,value,selectEl) {
  const u=await fetch(`/api/sessions/${id}`,{
    method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({[field]:value})
  }).then(r=>r.json());
  if (field==='vehicle'&&selectEl) {
    const vn=value.toLowerCase();
    selectEl.className='vehicle-select '+(vn.includes('tesla')?'tesla':(vn.includes('shark')||vn.includes('byd'))?'shark':vn.includes('leaf')?'leaf':'');
    const cells=selectEl.closest('tr').querySelectorAll('td');
    if (cells[7]) cells[7].textContent='$'+(u.rate_kwh||0).toFixed(4);
    if (cells[8]) cells[8].textContent='$'+(u.cost||0).toFixed(2);
  }
}

function patchSoc(id, field, value) {
  const v = value.trim() === '' ? null : +value;
  patchSession(id, field, v, null);
}

// ── Config ────────────────────────────────────────────────────────────────────
async function loadConfig() {
  try {
    const c=await fetch('/api/config').then(r=>r.json());
    vehicleNames=(c.vehicles||[]).map(v=>v.name);
    if (!vehicleNames.includes('Unknown')) vehicleNames.push('Unknown');

    vehicleCapacities={};
    for (const v of (c.vehicles||[])) vehicleCapacities[v.name]=v.capacity_kwh||0;

    const filter=document.getElementById('vehicle-filter');
    const prev=filter.value;
    filter.innerHTML='<option value="">All vehicles</option>'+
      vehicleNames.map(n=>`<option value="${n}">${n}</option>`).join('');
    filter.value=prev;

    rebuildStatsVehicleButtons();

    const note=document.getElementById('rate-note');
    if (note&&c.vehicles) {
      const s=c.offpeak_start_hour, e=c.offpeak_end_hour;
      const fH=h=>h===0?'12am':h<12?`${h}am`:h===12?'12pm':`${h-12}pm`;
      const evVeh=c.vehicles.filter(v=>v.ev_powerup).map(v=>v.name).join(', ');
      note.textContent=
        (evVeh?`${evVeh} off-peak (${fH(s)}–${fH(e)}): $${c.rate_ev_powerup_kwh.toFixed(3)}/kWh  •  `:'')+
        `General: $${c.rate_general_kwh.toFixed(4)}/kWh`;
    }

    updateSOC();
  } catch(e) { console.error('loadConfig:',e); }
}

// ── Efficiency calibration ───────────────────────────────────────────────────
async function loadEfficiency() {
  try {
    const all = await fetch('/api/sessions').then(r => r.json());
    vehicleEfficiency = {};
    for (const name of vehicleNames) {
      const cap = vehicleCapacities[name] || 0;
      if (!cap) continue;
      const calibrated = all.filter(s =>
        s.vehicle === name &&
        s.start_soc != null && s.end_soc != null &&
        s.end_soc > s.start_soc && s.energy_wh > 0
      );
      if (!calibrated.length) continue;
      const etas = calibrated.map(s => {
        const stored = (s.end_soc - s.start_soc) / 100 * cap;
        return stored / (s.energy_wh / 1000);
      }).filter(e => e > 0.5 && e <= 1.05);
      if (etas.length) {
        vehicleEfficiency[name] = {
          eta:   etas.reduce((a,b) => a+b) / etas.length,
          count: etas.length,
        };
      }
    }
  } catch(e) { console.error('loadEfficiency:', e); }
}

// ── Stats chart ───────────────────────────────────────────────────────────────
function rebuildStatsVehicleButtons() {
  const wrap=document.getElementById('stats-vehicle-btns');
  wrap.innerHTML=`<button class="pill ${statsVehicle===''?'active':''}" id="statsv-all" onclick="setStatsVehicle('')">All</button>`;
  const sel=document.getElementById('stats-vehicle-sel');
  if (sel) sel.innerHTML='<option value="">All vehicles</option>';
  for (const name of vehicleNames) {
    if (name==='Unknown') continue;
    const btn=document.createElement('button');
    btn.className='pill'+(statsVehicle===name?' active':'');
    btn.id='statsv-'+name.replace(/\s+/g,'-');
    btn.textContent=name;
    btn.onclick=()=>setStatsVehicle(name);
    wrap.appendChild(btn);
    if (sel) {
      const opt=document.createElement('option');
      opt.value=name; opt.textContent=name;
      if (name===statsVehicle) opt.selected=true;
      sel.appendChild(opt);
    }
  }
}

function setStatsRange(range) {
  statsRange = range;
  ['month','year','all'].forEach(r=>{
    const el = document.getElementById('stats-'+r);
    if (el) el.classList.toggle('active', r===range);
  });
  const sel = document.getElementById('stats-range-sel');
  if (sel) sel.value = range;
  updateStatsNav();
  loadStats();
}
function updateStatsNav() {
  const nav = document.getElementById('stats-nav');
  const lbl = document.getElementById('stats-nav-label');
  if (!nav) return;
  nav.style.display = statsRange !== 'all' ? 'flex' : 'none';
  if (statsRange === 'month') {
    lbl.textContent = new Date(statsNavYear, statsNavMonth, 1)
      .toLocaleDateString('en-AU', {month:'long', year:'numeric'});
  } else if (statsRange === 'year') {
    lbl.textContent = String(statsNavYear);
  }
}
function statsNavPrev() {
  if (statsRange === 'month') {
    statsNavMonth--;
    if (statsNavMonth < 0) { statsNavMonth = 11; statsNavYear--; }
  } else {
    statsNavYear--;
  }
  updateStatsNav(); loadStats();
}
function statsNavNext() {
  if (statsRange === 'month') {
    statsNavMonth++;
    if (statsNavMonth > 11) { statsNavMonth = 0; statsNavYear++; }
  } else {
    statsNavYear++;
  }
  updateStatsNav(); loadStats();
}

function setStatsVehicle(v) {
  statsVehicle=v;
  document.querySelectorAll('#stats-vehicle-btns .pill').forEach(b=>b.classList.remove('active'));
  const id=v?'statsv-'+v.replace(/\s+/g,'-'):'statsv-all';
  const el=document.getElementById(id);
  if (el) el.classList.add('active');
  const sel=document.getElementById('stats-vehicle-sel');
  if (sel) sel.value=v;
  _renderStats();
}

async function loadStats() {
  let url='/api/sessions';
  if (statsRange==='month') {
    url+=`?month=${statsNavYear}-${String(statsNavMonth+1).padStart(2,'0')}`;
  }
  const all=await fetch(url).then(r=>r.json());
  if (statsRange==='year') {
    _statsAllData=all.filter(s=>s.start_time&&new Date(s.start_time).getFullYear()===statsNavYear);
  } else {
    _statsAllData=all;
  }
  _renderStats();
}

function _renderStats() {
  const sessions=statsVehicle?_statsAllData.filter(s=>s.vehicle===statsVehicle):_statsAllData;
  const C=cc();
  let labels=[], data=[], colors=[], tipData=[];

  if (statsRange==='month') {
    // One bar per calendar day in the selected month
    const yr=statsNavYear, mo=statsNavMonth;
    const daysInMonth=new Date(yr,mo+1,0).getDate();
    const buckets={};
    for (const s of sessions) {
      if (!s.start_time) continue;
      const d=new Date(s.start_time);
      if (d.getFullYear()!==yr||d.getMonth()!==mo) continue;
      const day=d.getDate();
      if (!buckets[day]) buckets[day]={wh:0,cost:0,sessions:[]};
      buckets[day].wh+=s.energy_wh||0;
      buckets[day].cost+=s.cost||0;
      buckets[day].sessions.push(s);
    }
    for (let d=1;d<=daysInMonth;d++) {
      labels.push(d);
      const b=buckets[d];
      data.push(b?+(b.wh/1000).toFixed(2):0);
      if (b&&b.sessions.length===1) colors.push(vehicleColor(b.sessions[0].vehicle));
      else if (b) colors.push(C.tick);
      else colors.push('rgba(0,0,0,0)');
      tipData.push(b||null);
    }
  } else {
    // One bar per calendar month (YTD or All Time)
    const monthMap={};
    for (const s of sessions) {
      if (!s.start_time) continue;
      const d=new Date(s.start_time);
      const key=`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}`;
      if (!monthMap[key]) monthMap[key]={wh:0,cost:0,sessions:[],d};
      monthMap[key].wh+=s.energy_wh||0;
      monthMap[key].cost+=s.cost||0;
      monthMap[key].sessions.push(s);
    }
    for (const key of Object.keys(monthMap).sort()) {
      const b=monthMap[key];
      labels.push(b.d.toLocaleDateString('en-AU',{month:'short',year:statsRange==='all'?'2-digit':undefined}));
      data.push(+(b.wh/1000).toFixed(2));
      const vs=[...new Set(b.sessions.map(s=>s.vehicle))];
      colors.push(vs.length===1?vehicleColor(vs[0]):C.tick);
      tipData.push(b);
    }
  }

  const ctx=document.getElementById('stats-chart').getContext('2d');
  if (statsChart) statsChart.destroy();
  statsChart=new Chart(ctx,{
    type:'bar',
    data:{labels,datasets:[{data,backgroundColor:colors,borderRadius:3,borderSkipped:false}]},
    options:{
      responsive:true,maintainAspectRatio:false,animation:false,devicePixelRatio:3,
      layout:{padding:{left:0,right:14}},
      plugins:{
        legend:{display:false},
        tooltip:{
          backgroundColor:C.tip.bg,borderColor:C.tip.border,borderWidth:1,
          titleColor:C.tip.title,bodyColor:C.tip.body,
          filter:item=>item.parsed.y>0,
          callbacks:{
            title:items=>{
              if (statsRange==='month') {
                return new Date(statsNavYear,statsNavMonth,items[0].label)
                  .toLocaleDateString('en-AU',{weekday:'short',day:'numeric',month:'short'});
              }
              return items[0].label;
            },
            label:item=>{
              const b=tipData[item.dataIndex];
              if (!b) return '';
              const lines=[`${item.parsed.y.toFixed(2)} kWh`];
              if (b.cost) lines.push('$'+b.cost.toFixed(2));
              const vs=[...new Set(b.sessions.map(s=>s.vehicle).filter(v=>v&&v!=='Unknown'))];
              if (vs.length) lines.push(vs.join(', '));
              if (b.sessions.length>1) lines.push(`${b.sessions.length} sessions`);
              return lines;
            }
          }
        }
      },
      scales:{
        x:{ticks:{color:C.tick,maxTicksLimit:31,maxRotation:0,font:{size:10}},grid:{display:false}},
        y:{title:{display:true,text:'kWh',color:C.tick,font:{size:11}},
           ticks:{color:C.tick,font:{size:11}},grid:{color:C.grid},min:0}
      }
    }
  });
}

// ── Live chart ────────────────────────────────────────────────────────────────
async function loadLiveSamples(sid) {
  try {
    const s=await fetch(`/api/sessions/${sid}/samples`).then(r=>r.json());
    _liveSamplesCache=s; renderLiveChart(s);
  } catch(e) {}
}
function switchLiveAxis(axis) {
  liveAxis=axis;
  document.getElementById('live-btn-energy').classList.toggle('active',axis==='energy');
  document.getElementById('live-btn-time').classList.toggle('active',axis==='time');
}
function renderLiveChart(samples) {
  const C=cc(), pts=samples.filter(s=>s.power_w!==null&&s.power_w>0);
  if (!pts.length) return;
  const base=samples[0];
  let labels,xLabel;
  if (liveAxis==='energy') {
    labels=pts.map(s=>((s.energy_wh-(base.energy_wh||0))/1000).toFixed(2)); xLabel='Energy delivered (kWh)';
  } else {
    labels=pts.map(s=>new Date(s.ts).toLocaleTimeString('en-AU',{hour:'2-digit',minute:'2-digit'})); xLabel='Time';
  }
  const ctx=document.getElementById('live-chart').getContext('2d');
  if (liveChart) {
    liveChart.data.labels=labels; liveChart.data.datasets[0].data=pts.map(s=>s.power_w);
    liveChart.options.scales.x.title.text=xLabel; liveChart.update('none'); return;
  }
  liveChart=new Chart(ctx,{type:'line',
    data:{labels,datasets:[{data:pts.map(s=>s.power_w),borderColor:'#00c853',
      backgroundColor:'rgba(0,200,83,0.06)',borderWidth:2,tension:0.35,pointRadius:2,pointHoverRadius:4}]},
    options:{responsive:true,maintainAspectRatio:false,animation:false,
      plugins:{legend:{display:false},tooltip:{backgroundColor:C.tip.bg,borderColor:C.tip.border,
        borderWidth:1,titleColor:C.tip.title,bodyColor:C.tip.body,
        callbacks:{label:c=>`${(c.parsed.y/1000).toFixed(2)} kW`}}},
      scales:{
        x:{title:{display:true,text:xLabel,color:C.tick,font:{size:11}},ticks:{color:C.tick,maxTicksLimit:8},grid:{color:C.grid}},
        y:{title:{display:true,text:'Power (W)',color:C.tick,font:{size:11}},
           ticks:{color:C.tick,callback:v=>v>=1000?(v/1000).toFixed(1)+'k':v},grid:{color:C.grid},min:0}
      }
    }
  });
}

// ── Trend chart ───────────────────────────────────────────────────────────────
async function showChart(sessionId,vehicle,totalKwh) {
  chartSamples=await fetch(`/api/sessions/${sessionId}/samples`).then(r=>r.json());
  document.getElementById('chart-modal').style.display='flex';
  document.getElementById('chart-title').textContent=`Session #${sessionId} — ${vehicle} — ${totalKwh.toFixed(2)} kWh`;
  chartAxis='energy';
  document.getElementById('btn-energy').classList.add('active');
  document.getElementById('btn-time').classList.remove('active');
  document.getElementById('chart-footnote').textContent='Calculated from 30-second energy samples · X-axis approximates SOC progression';
  renderChart();
}
function switchAxis(axis) {
  chartAxis=axis;
  document.getElementById('btn-energy').classList.toggle('active',axis==='energy');
  document.getElementById('btn-time').classList.toggle('active',axis==='time');
  document.getElementById('chart-footnote').textContent=axis==='energy'
    ?'Calculated from 30-second energy samples · X-axis approximates SOC progression'
    :'Calculated from 30-second energy samples';
  renderChart();
}
function renderChart() {
  const C=cc();
  if (!chartSamples.length) {
    document.getElementById('chart-sub').textContent='No sample data.';
    if (chartInstance) { chartInstance.destroy(); chartInstance=null; } return;
  }

  // Build unified dataset: real samples (power>0) plus gap-fill zeros at poll cadence.
  // Each point carries both axes so both views use the same underlying data.
  const GAP_MS=90000, POLL_MIN=0.5;
  const t0=new Date(chartSamples[0].ts).getTime();
  const base=chartSamples[0].energy_wh||0;
  const allPts=[]; // {xTime, xEnergy, y}

  for (let i=0;i<chartSamples.length;i++) {
    const s=chartSamples[i], tMs=new Date(s.ts).getTime();
    const xMin=(tMs-t0)/60000;
    const xKwh=((s.energy_wh||0)-base)/1000;
    if (i>0) {
      const prev=chartSamples[i-1];
      const prevMs=new Date(prev.ts).getTime();
      const prevX=(prevMs-t0)/60000;
      const prevKwh=((prev.energy_wh||0)-base)/1000;
      if (tMs-prevMs>GAP_MS) {
        // Fill gap: time advances at poll rate; energy stays pinned at prevKwh (no delivery).
        for (let gx=prevX+POLL_MIN; gx<xMin-POLL_MIN/2; gx+=POLL_MIN) {
          allPts.push({xTime:+gx.toFixed(4), xEnergy:prevKwh, y:0});
        }
      }
    }
    if (s.power_w!=null&&s.power_w>0) allPts.push({xTime:xMin, xEnergy:xKwh, y:s.power_w});
  }

  const activeCnt=allPts.filter(p=>p.y>0).length;
  if (!activeCnt) {
    document.getElementById('chart-sub').textContent='No sample data.';
    if (chartInstance) { chartInstance.destroy(); chartInstance=null; } return;
  }
  const maxP=Math.max(...allPts.map(p=>p.y));
  document.getElementById('chart-sub').textContent=`${activeCnt} samples · peak ${(maxP/1000).toFixed(1)} kW`;

  const isTime=chartAxis==='time';
  const data=allPts.map(p=>({x:isTime?p.xTime:p.xEnergy, y:p.y}));
  const xLabel=isTime?'Time':'Energy Delivered (kWh) — SOC proxy';

  const ctx=document.getElementById('trend-chart').getContext('2d');
  if (chartInstance) chartInstance.destroy();
  chartInstance=new Chart(ctx,{type:'line',
    data:{datasets:[{label:'Power (W)',data,borderColor:'#00c853',
      backgroundColor:'rgba(0,200,83,0.08)',borderWidth:2,tension:0.35,
      pointRadius:0,pointHoverRadius:0}]},
    options:{responsive:true,maintainAspectRatio:false,devicePixelRatio:3,
      interaction:{mode:'index',intersect:false},
      plugins:{legend:{display:false},tooltip:{backgroundColor:C.tip.bg,borderColor:C.tip.border,
        borderWidth:1,titleColor:C.tip.title,bodyColor:C.tip.body,
        callbacks:{
          title:items=>{
            if (!isTime) return items[0].parsed.x.toFixed(2)+' kWh';
            const d=new Date(t0+items[0].parsed.x*60000);
            return d.toLocaleTimeString('en-AU',{hour:'2-digit',minute:'2-digit'});
          },
          label:c=>c.parsed.y?`${(c.parsed.y/1000).toFixed(2)} kW`:''
        }}},
      scales:{
        x: isTime ? {
          type:'linear',
          min:0, max:allPts.length?allPts[allPts.length-1].xTime:undefined,
          title:{display:true,text:xLabel,color:C.tick,font:{size:11}},
          afterBuildTicks:scale=>{
            // Snap ticks to whole clock hours within the visible range
            const minMs=t0+scale.min*60000, maxMs=t0+scale.max*60000;
            const s=new Date(minMs); s.setMinutes(0,0,0);
            if(s.getTime()<minMs) s.setHours(s.getHours()+1);
            const tks=[]; let cur=s.getTime();
            while(cur<=maxMs){tks.push({value:(cur-t0)/60000});cur+=3600000;}
            if(tks.length) scale.ticks=tks;
          },
          ticks:{color:C.tick,callback:v=>{
            const d=new Date(t0+v*60000);
            return d.toLocaleTimeString('en-AU',{hour:'2-digit',minute:'2-digit'});
          }},
          grid:{color:C.grid}
        } : {
          type:'linear',
          title:{display:true,text:xLabel,color:C.tick,font:{size:11}},
          ticks:{color:C.tick,maxTicksLimit:10,callback:v=>v.toFixed(1)},
          grid:{color:C.grid}
        },
        y:{title:{display:true,text:'Power (W)',color:C.tick,font:{size:11}},
           ticks:{color:C.tick,callback:v=>v>=1000?(v/1000).toFixed(1)+'k':v},
           grid:{color:C.grid},min:0}
      }
    }
  });
}
function showToast(msg,ms=2200){
  const t=document.getElementById('toast');
  t.textContent=msg; t.style.display='block';
  clearTimeout(t._t);
  t._t=setTimeout(()=>t.style.display='none',ms);
}
function saveChartImage(){
  const box=document.querySelector('.modal-box');
  const bg=document.documentElement.dataset.theme==='dark'?'#1e1e1e':'#ffffff';
  // Hide UI chrome so it doesn't appear in the exported image
  const hide=[document.querySelector('.modal-close'),document.getElementById('btn-save-chart')];
  hide.forEach(el=>{if(el)el.style.visibility='hidden';});
  html2canvas(box,{backgroundColor:bg,scale:3,useCORS:true}).then(canvas=>{
    hide.forEach(el=>{if(el)el.style.visibility='';});
    canvas.toBlob(blob=>{
      if(navigator.clipboard&&window.ClipboardItem){
        navigator.clipboard.write([new ClipboardItem({'image/png':blob})])
          .then(()=>showToast('Copied to clipboard ✓'))
          .catch(()=>dlImg(blob));
      } else { dlImg(blob); }
    },'image/png');
  }).catch(()=>hide.forEach(el=>{if(el)el.style.visibility='';}));
}
function dlImg(blob){
  const title=(document.getElementById('chart-title').textContent||'charging-session')
    .replace(/[^\w\s-]/g,'').trim().replace(/\s+/g,'-');
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download=title+'.png';
  a.click(); URL.revokeObjectURL(a.href);
  showToast('Chart saved as PNG ✓');
}
function saveStatsImage(){
  const canvas=document.getElementById('stats-chart');
  const label=document.getElementById('stats-nav-label').textContent||'charging-history';
  const tmp=document.createElement('canvas');
  tmp.width=canvas.width; tmp.height=canvas.height;
  const g=tmp.getContext('2d');
  g.fillStyle=document.documentElement.dataset.theme==='dark'?'#1e1e1e':'#ffffff';
  g.fillRect(0,0,tmp.width,tmp.height);
  g.drawImage(canvas,0,0);
  tmp.toBlob(blob=>{
    if(navigator.clipboard&&window.ClipboardItem){
      navigator.clipboard.write([new ClipboardItem({'image/png':blob})])
        .then(()=>showToast('Histogram copied to clipboard ✓'))
        .catch(()=>dlStatsImg(blob,label));
    } else { dlStatsImg(blob,label); }
  },'image/png');
}
function dlStatsImg(blob,label){
  const name=(label||'charging-history').replace(/[^\w\s-]/g,'').trim().replace(/\s+/g,'-');
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download=name+'.png';
  a.click(); URL.revokeObjectURL(a.href);
  showToast('Histogram saved as PNG ✓');
}
async function exportCSV(){
  let url='/api/sessions';
  const p=[];
  if(currentDays) p.push('days='+currentDays);
  if(currentVehicle) p.push('vehicle='+encodeURIComponent(currentVehicle));
  if(p.length) url+='?'+p.join('&');
  const rows=await fetch(url).then(r=>r.json());
  const q=v=>'"'+String(v==null?'':v).replace(/"/g,'""')+'"';
  const hdr=['#','Date','Start Time','End Time','Vehicle','Energy (kWh)','Avg Power (kW)',
             'Duration (min)','Rate ($/kWh)','Cost ($)','SOC Start (%)','SOC End (%)','Notes'];
  const lines=[hdr.map(q).join(',')];
  for(const r of rows){
    const dt=r.start_time?new Date(r.start_time):null;
    const et=r.end_time?new Date(r.end_time):null;
    const wh=r.energy_wh||0, dur=r.duration_s||0;
    const avgKw=(wh&&dur>10)?+(wh/(dur/3600)/1000).toFixed(2):'';
    lines.push([
      r.id,
      dt?dt.toLocaleDateString('en-AU'):'',
      dt?dt.toLocaleTimeString('en-AU',{hour:'2-digit',minute:'2-digit'}):'',
      et?et.toLocaleTimeString('en-AU',{hour:'2-digit',minute:'2-digit'}):'',
      r.vehicle||'',
      wh?(wh/1000).toFixed(2):'',
      avgKw,
      dur?+(dur/60).toFixed(1):'',
      (r.rate_kwh||0).toFixed(4),
      (r.cost||0).toFixed(2),
      r.start_soc??'',
      r.end_soc??'',
      r.notes||''
    ].map(q).join(','));
  }
  const blob=new Blob(['﻿'+lines.join('\r\n')],{type:'text/csv;charset=utf-8'});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download='charging-'+new Date().toISOString().slice(0,10)+'.csv';
  a.click(); URL.revokeObjectURL(a.href);
  showToast('Exported '+rows.length+' session'+(rows.length===1?'':'s')+' ✓');
}
function closeChart() {
  document.getElementById('chart-modal').style.display='none';
  if (chartInstance) { chartInstance.destroy(); chartInstance=null; }
}
document.getElementById('chart-modal').addEventListener('click',e=>{
  if (e.target===document.getElementById('chart-modal')) closeChart();
});

// ── Init ──────────────────────────────────────────────────────────────────────
loadConfig().then(loadEfficiency);
loadStatus();
loadSessions();
updateStatsNav(); loadStats();
loadRecentPerVehicle();
setInterval(loadStatus,           30000);
setInterval(loadSessions,         60000);
setInterval(loadStats,           120000);
setInterval(loadRecentPerVehicle, 120000);
setInterval(loadEfficiency,       300000);
</script>
</body>
</html>
"""


SETTINGS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wall Connector — Settings</title>
<script>(function(){const t=localStorage.getItem('wc-theme');if(t)document.documentElement.dataset.theme=t})();</script>
<style>
:root{
  --bg:#161616;--bg2:#1e1e1e;--bg3:#252525;
  --border:#2d2d2d;--border2:#383838;
  --text:#f0f0f0;--text2:#aaa;--label:#777;
  --blue:#64b5f6;--blue-bg:#1a3a5c;--blue-bd:#1e5a9c;
}
[data-theme=light]{
  --bg:#f0f2f5;--bg2:#fff;--bg3:#f5f5f5;
  --border:#e0e0e0;--border2:#d0d0d0;
  --text:#111;--text2:#555;--label:#888;
  --blue:#1565c0;--blue-bg:#dbeafe;--blue-bd:#93c5fd;
}
@media(prefers-color-scheme:light){:root:not([data-theme=dark]){
  --bg:#f0f2f5;--bg2:#fff;--bg3:#f5f5f5;
  --border:#e0e0e0;--border2:#d0d0d0;
  --text:#111;--text2:#555;--label:#888;
  --blue:#1565c0;--blue-bg:#dbeafe;--blue-bd:#93c5fd;
}}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:14px}
header{padding:12px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px}
a.back{color:var(--label);text-decoration:none;font-size:13px;padding:5px 11px;border:1px solid var(--border2);border-radius:5px;transition:all .15s}
a.back:hover{color:var(--text);border-color:var(--border)}
h1{font-size:15px;font-weight:600;color:var(--text);flex:1}
.icon-btn{background:none;border:1px solid var(--border2);color:var(--label);width:30px;height:30px;border-radius:6px;cursor:pointer;font-size:14px;display:flex;align-items:center;justify-content:center;transition:all .15s}
.icon-btn:hover{color:var(--text2);border-color:var(--border)}
.container{max-width:740px;margin:0 auto;padding:20px}
h2{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--label);margin:24px 0 10px}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:14px 18px}
.field{display:grid;grid-template-columns:210px 1fr;gap:8px;align-items:center;padding:8px 0;border-bottom:1px solid var(--border)}
.field:last-child{border-bottom:none}
.field label{font-size:12px;color:var(--text2)}
input[type=text],input[type=number]{background:var(--bg3);border:1px solid var(--border2);color:var(--text);padding:6px 10px;border-radius:4px;font-size:13px;width:100%}
input:focus{outline:none;border-color:var(--blue-bd)}

/* Vehicle table */
.v-header{display:grid;grid-template-columns:1fr 76px 80px 80px 32px;gap:8px;margin-bottom:4px;padding:0 2px}
.v-header span{font-size:11px;color:var(--label)}
.vehicle-row{display:grid;grid-template-columns:1fr 76px 80px 80px 32px;gap:8px;align-items:center;margin-bottom:8px}
.check-wrap{display:flex;align-items:center;gap:5px;font-size:12px;color:var(--text2);justify-content:center}
input[type=checkbox]{accent-color:#00c853;width:15px;height:15px;cursor:pointer;flex-shrink:0}
.del-btn{background:var(--bg3);border:1px solid var(--border2);color:var(--label);width:28px;height:28px;border-radius:4px;cursor:pointer;font-size:16px;line-height:1;display:flex;align-items:center;justify-content:center}
.del-btn:hover{border-color:#c00;color:#f44}
.add-btn{background:var(--bg3);border:1px solid var(--border2);color:var(--text2);padding:6px 14px;border-radius:4px;cursor:pointer;font-size:12px;margin-top:8px}
.add-btn:hover{border-color:var(--border);color:var(--text)}
.save-btn{background:var(--blue-bg);border:1px solid var(--blue-bd);color:var(--blue);padding:8px 24px;border-radius:5px;cursor:pointer;font-size:13px;font-weight:600;margin-top:18px}
.save-btn:hover{opacity:.85}
.status{font-size:12px;color:#00c853;margin-top:8px;min-height:18px}
.note{font-size:11px;color:var(--label);margin-top:6px;line-height:1.5}

@media(max-width:640px){
  .field{grid-template-columns:1fr;gap:4px}
  .container{padding:16px}
}
@media(max-width:540px){
  .v-header,.vehicle-row{grid-template-columns:1fr 64px 70px 70px 28px;gap:6px}
}
@media(max-width:480px){
  .v-header{display:none}
  .vehicle-row{
    display:flex;flex-wrap:wrap;gap:6px;
    padding-bottom:10px;margin-bottom:6px;
    border-bottom:1px solid var(--border)
  }
  .vehicle-row input[type=text]{order:1;flex:1 1 auto;min-width:0}
  .vehicle-row .del-btn{order:2;flex:0 0 auto;align-self:flex-start}
  .vehicle-row input[type=number]{order:3;flex:1 1 70px;min-width:60px}
  .vehicle-row .check-wrap{order:4;flex:0 0 auto;align-self:center}
}
</style>
</head>
<body>
<header>
  <a class="back" href="/">← Dashboard</a>
  <h1>Settings</h1>
  <button class="icon-btn" id="theme-btn" onclick="toggleTheme()" title="Toggle theme">◑</button>
</header>
<div class="container">

  <h2>Vehicles</h2>
  <p class="note" style="margin-bottom:10px">Auto-detected by average charge power after 2 min. The lowest-capacity vehicle whose max power × 1.2 ≥ observed power is chosen.</p>
  <div class="card">
    <div class="v-header">
      <span>Name</span>
      <span>Max (kW)</span>
      <span>Battery (kWh)</span>
      <span style="text-align:center">EV off-peak</span>
      <span></span>
    </div>
    <div id="vehicles-list"></div>
    <button class="add-btn" onclick="addVehicle()">+ Add vehicle</button>
  </div>
  <p class="note">Max power is used for auto-detection. Battery capacity is used for the live SOC gauge.</p>

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
  <p class="note">e.g. Start 22, End 7 = 10 pm to 7 am. Applied to vehicles with EV off-peak enabled.</p>

  <button class="save-btn" onclick="saveConfig()">Save</button>
  <div class="status" id="status"></div>

  <h2>Charger</h2>
  <div class="card">
    <div class="field">
      <label>Wall Connector IP</label>
      <input type="text" id="wc_ip" disabled style="color:var(--label)">
    </div>
  </div>
  <p class="note">IP address requires a server restart to change (edit config.json).</p>

</div>
<script>
function isLight() {
  const t = document.documentElement.dataset.theme;
  return t==='light' || (!t && window.matchMedia('(prefers-color-scheme: light)').matches);
}
function toggleTheme() {
  document.documentElement.dataset.theme = isLight() ? 'dark' : 'light';
  localStorage.setItem('wc-theme', document.documentElement.dataset.theme);
  updateThemeBtn();
}
function updateThemeBtn() {
  const btn = document.getElementById('theme-btn');
  if (btn) btn.textContent = isLight() ? '☾' : '☀';
}
updateThemeBtn();

let cfg = {};

async function load() {
  cfg = await fetch('/api/config').then(r => r.json());
  document.getElementById('rate_general').value    = cfg.rate_general_kwh    || 0;
  document.getElementById('rate_ev_powerup').value = cfg.rate_ev_powerup_kwh || 0;
  document.getElementById('offpeak_start').value   = cfg.offpeak_start_hour  ?? 22;
  document.getElementById('offpeak_end').value     = cfg.offpeak_end_hour    ?? 7;
  document.getElementById('wc_ip').value           = cfg.wc_ip || '';
  renderVehicles(cfg.vehicles || []);
}

function renderVehicles(vehicles) {
  const list = document.getElementById('vehicles-list');
  list.innerHTML = '';
  vehicles.forEach((v, i) => {
    const maxKw = v.max_power_w ? (v.max_power_w / 1000).toFixed(1) : '';
    const capKwh = v.capacity_kwh || '';
    const row = document.createElement('div');
    row.className = 'vehicle-row';
    row.innerHTML = `
      <input type="text"   value="${v.name||''}"   oninput="updateV(${i},'name',this.value)"                              placeholder="Name">
      <input type="number" value="${maxKw}"         oninput="updateV(${i},'max_power_w',Math.round(+this.value*1000))"    placeholder="kW"  min="0" step="0.1">
      <input type="number" value="${capKwh}"        oninput="updateV(${i},'capacity_kwh',+this.value)"                    placeholder="kWh" min="0" step="0.1">
      <div class="check-wrap">
        <input type="checkbox" ${v.ev_powerup?'checked':''} onchange="updateV(${i},'ev_powerup',this.checked)">
      </div>
      <button class="del-btn" onclick="removeV(${i})">×</button>`;
    list.appendChild(row);
  });
}

function updateV(i, key, val) { cfg.vehicles[i][key] = val; }
function removeV(i) { cfg.vehicles.splice(i,1); renderVehicles(cfg.vehicles); }
function addVehicle() {
  if (!cfg.vehicles) cfg.vehicles = [];
  cfg.vehicles.push({name:'', max_power_w:0, capacity_kwh:0, ev_powerup:false});
  renderVehicles(cfg.vehicles);
}

async function saveConfig() {
  const payload = {
    rate_general_kwh:    +document.getElementById('rate_general').value,
    rate_ev_powerup_kwh: +document.getElementById('rate_ev_powerup').value,
    offpeak_start_hour:  +document.getElementById('offpeak_start').value,
    offpeak_end_hour:    +document.getElementById('offpeak_end').value,
    vehicles: cfg.vehicles,
  };
  const r  = await fetch('/api/config',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const st = document.getElementById('status');
  if (r.ok) {
    st.textContent='Saved.'; st.style.color='#00c853';
    setTimeout(()=>st.textContent='', 3000);
  } else {
    st.textContent='Error saving.'; st.style.color='#f44';
  }
}

load();
</script>
</body>
</html>
"""


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
    recalc_session_durations()

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
