#!/usr/bin/env python3
"""Tesla Gen 3 Wall Connector — session tracker.

The original single-file version of this project, kept for reference. It has
been superseded by wc_server.py (polling, storage, dashboard and REST API) and
wc_client.py (command line against that API), which is what you should run.
Settings here are module constants rather than config.json, and it has no
dashboard, no session merging and no multi-vehicle support.

Commands:
  daemon   Poll charger every 30s, log charging sessions to SQLite
  status   Show current charger state
  report   Print session history with cost totals
  tag      Tag a session with vehicle name (tesla / shark)
  note     Add a note to a session
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

# ── Config ───────────────────────────────────────────────────────────────────

WC_IP          = "192.168.1.100"
VITALS_URL     = f"http://{WC_IP}/api/1/vitals"
LIFETIME_URL   = f"http://{WC_IP}/api/1/lifetime"
POLL_INTERVAL  = 30          # seconds between polls

# Origin Energy rates (AUD/kWh incl GST, post 1-Jul-2025)
RATE_GENERAL   = 0.352440   # General usage
RATE_CL2       = 0.177650   # Controlled Load 2 (off-peak)
DEFAULT_RATE   = RATE_GENERAL

# Auto-detect vehicle from average charge power
# Shark: single-phase 6.5 kW   Tesla: three-phase 11 kW   midpoint: 8.75 kW
VEHICLE_POWER_THRESHOLD_W = 8750
AUTO_TAG_AFTER_S          = 120   # wait 2 min before auto-tagging

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wc_sessions.db")


# ── Database ─────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            start_time   TEXT NOT NULL,
            end_time     TEXT,
            duration_s   INTEGER,
            energy_wh    REAL,
            vehicle      TEXT DEFAULT 'Unknown',
            auto_tagged  INTEGER DEFAULT 0,
            rate_kwh     REAL DEFAULT 0.352440,
            notes        TEXT
        )
    """)
    conn.commit()
    return conn


# ── API helpers ───────────────────────────────────────────────────────────────

def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return None


# ── Formatting ────────────────────────────────────────────────────────────────

def fmt_duration(seconds):
    if seconds is None or seconds == 0:
        return "—"
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    return f"{h}h {m:02d}m" if h else f"{m}m"


def fmt_energy(wh):
    return f"{wh / 1000:.2f}" if wh else "—"


def fmt_cost(wh, rate):
    return f"${wh / 1000 * rate:.2f}" if wh else "—"


def fmt_power(wh, seconds):
    if not wh or not seconds or seconds < 10:
        return "—"
    return f"{wh / (seconds / 3600):.0f} W"


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_status(_args):
    v  = fetch(VITALS_URL)
    lt = fetch(LIFETIME_URL)

    if v is None:
        print(f"ERROR: Cannot reach Wall Connector at {WC_IP}")
        return

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n  Wall Connector  —  {ts}")
    print(f"  {'Grid':22s} {v['grid_v']:.1f} V / {v['grid_hz']:.2f} Hz")
    print(f"  {'Vehicle connected':22s} {'Yes' if v['vehicle_connected'] else 'No'}")
    print(f"  {'Charging':22s} {'Yes' if v['contactor_closed'] else 'No'}")

    if v['contactor_closed']:
        energy_wh = v.get('session_energy_wh', 0)
        duration_s = v.get('session_s', 0)
        power_w = energy_wh / (duration_s / 3600) if duration_s > 10 else None
        vehicle_guess = ""
        if power_w:
            vehicle_guess = "  (→ Tesla)" if power_w > VEHICLE_POWER_THRESHOLD_W else "  (→ Shark)"
        print(f"  {'Session energy':22s} {fmt_energy(energy_wh)} kWh")
        print(f"  {'Session duration':22s} {fmt_duration(duration_s)}")
        if power_w:
            print(f"  {'Average power':22s} {power_w:.0f} W{vehicle_guess}")
        print(f"  {'Est. cost @ rate':22s} {fmt_cost(energy_wh, DEFAULT_RATE)}")

    print(f"  {'MCU temp':22s} {v['mcu_temp_c']:.1f}°C  "
          f"handle {v['handle_temp_c']:.1f}°C")

    if lt:
        print(f"\n  Lifetime totals")
        print(f"  {'Sessions':22s} {lt['charge_starts']:,}")
        print(f"  {'Energy':22s} {lt['energy_wh'] / 1000:.1f} kWh")
        print(f"  {'Charging time':22s} {lt['charging_time_s'] // 3600:,} hrs")
    print()


def cmd_daemon(args):
    rate = args.rate
    conn = get_db()

    session_id      = None
    was_charging    = False
    session_energy  = 0      # last known energy during session
    session_start   = None   # local datetime for duration fallback
    auto_tagged     = False

    print(f"Daemon started — polling every {POLL_INTERVAL}s  |  rate ${rate:.4f}/kWh  |  {DB_PATH}")
    print("Ctrl+C to stop.\n")

    def handle_exit(sig, _frame):
        print("\nDaemon stopped.")
        conn.close()
        sys.exit(0)

    signal.signal(signal.SIGINT,  handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    while True:
        v       = fetch(VITALS_URL)
        now_utc = datetime.now(timezone.utc)
        now_iso = now_utc.isoformat()
        ts      = datetime.now().strftime("%H:%M:%S")

        if v is None:
            print(f"[{ts}] Charger unreachable — retrying in {POLL_INTERVAL}s")
            time.sleep(POLL_INTERVAL)
            continue

        is_charging = bool(v.get("contactor_closed"))
        energy_wh   = v.get("session_energy_wh", 0) or 0
        session_s   = v.get("session_s", 0) or 0

        # ── Session start ──────────────────────────────────────────────────
        if is_charging and not was_charging:
            session_start  = now_utc
            session_energy = energy_wh
            auto_tagged    = False
            cur = conn.execute(
                "INSERT INTO sessions (start_time, rate_kwh, energy_wh, vehicle) VALUES (?, ?, ?, 'Unknown')",
                (now_iso, rate, energy_wh)
            )
            conn.commit()
            session_id = cur.lastrowid
            print(f"[{ts}] Session {session_id} started")

        # ── Daemon started while session in progress ───────────────────────
        elif is_charging and not session_id:
            session_start  = now_utc
            session_energy = energy_wh
            auto_tagged    = False
            cur = conn.execute(
                "INSERT INTO sessions (start_time, rate_kwh, energy_wh, vehicle) VALUES (?, ?, ?, 'Unknown')",
                (now_iso, rate, energy_wh)
            )
            conn.commit()
            session_id = cur.lastrowid
            print(f"[{ts}] Picked up in-progress session {session_id}")

        # ── Session in progress ────────────────────────────────────────────
        elif is_charging and session_id:
            if energy_wh > 0:
                session_energy = energy_wh

            # Use API session_s if available, else local clock
            duration = session_s if session_s > 0 else int((now_utc - session_start).total_seconds())

            conn.execute(
                "UPDATE sessions SET end_time=?, duration_s=?, energy_wh=? WHERE id=?",
                (now_iso, duration, session_energy, session_id)
            )
            conn.commit()

            # Auto-tag vehicle once we have enough data
            if not auto_tagged and duration >= AUTO_TAG_AFTER_S and session_energy > 0:
                avg_power_w = session_energy / (duration / 3600)
                vehicle = "Tesla" if avg_power_w > VEHICLE_POWER_THRESHOLD_W else "Shark"
                conn.execute(
                    "UPDATE sessions SET vehicle=?, auto_tagged=1 WHERE id=?",
                    (vehicle, session_id)
                )
                conn.commit()
                auto_tagged = True
                print(f"[{ts}] Session {session_id} auto-tagged → {vehicle}  ({avg_power_w:.0f} W avg)")

        # ── Session ended ──────────────────────────────────────────────────
        elif not is_charging and was_charging and session_id:
            # session_s may have reset to 0; use local clock as fallback
            duration = session_s if session_s > 0 else int((now_utc - session_start).total_seconds())

            # energy_wh may still hold last value, or use our tracked value
            final_energy = energy_wh if energy_wh > 0 else session_energy

            conn.execute(
                "UPDATE sessions SET end_time=?, duration_s=?, energy_wh=? WHERE id=?",
                (now_iso, duration, final_energy, session_id)
            )
            conn.commit()

            row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            e = row["energy_wh"] or 0
            d = row["duration_s"] or 0
            cost = e / 1000 * rate
            print(f"[{ts}] Session {session_id} ended — {e/1000:.2f} kWh  {fmt_duration(d)}  ${cost:.2f}  [{row['vehicle']}]")

            session_id     = None
            session_energy = 0
            session_start  = None
            auto_tagged    = False

        was_charging = is_charging
        time.sleep(POLL_INTERVAL)


def cmd_report(args):
    conn  = get_db()
    where = []
    params = []

    if args.days:
        where.append("start_time >= datetime('now', ?)")
        params.append(f"-{args.days} days")
    if args.month:
        where.append("strftime('%Y-%m', start_time) = ?")
        params.append(args.month)
    if args.vehicle:
        where.append("lower(vehicle) = lower(?)")
        params.append(args.vehicle)

    sql = "SELECT * FROM sessions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY start_time DESC"

    rows = conn.execute(sql, params).fetchall()

    if not rows:
        print("No sessions found.")
        return

    total_wh   = 0.0
    total_cost = 0.0
    by_vehicle: dict[str, dict] = {}

    hdr = f"  {'ID':>4}  {'Date':10}  {'Start':5}  {'Vehicle':7}  {'kWh':>6}  {'Avg W':>6}  {'Duration':>8}  {'Cost':>7}  Notes"
    print(f"\n{hdr}")
    print("  " + "─" * (len(hdr) - 2))

    for r in rows:
        energy_wh  = r["energy_wh"] or 0
        duration_s = r["duration_s"] or 0
        rate       = r["rate_kwh"] or DEFAULT_RATE
        cost       = energy_wh / 1000 * rate
        avg_w      = fmt_power(energy_wh, duration_s)
        vehicle    = r["vehicle"] or "Unknown"
        notes      = r["notes"] or ""
        auto       = "~" if r["auto_tagged"] else " "

        try:
            dt       = datetime.fromisoformat(r["start_time"]).astimezone()
            date_str = dt.strftime("%Y-%m-%d")
            time_str = dt.strftime("%H:%M")
        except Exception:
            date_str = r["start_time"][:10]
            time_str = r["start_time"][11:16]

        print(f"  {r['id']:>4}  {date_str}  {time_str}  {auto}{vehicle:<6}  "
              f"{energy_wh/1000:>6.2f}  {avg_w:>6}  {fmt_duration(duration_s):>8}  "
              f"{cost:>7.2f}  {notes}")

        if energy_wh:
            total_wh   += energy_wh
            total_cost += cost
            by_vehicle.setdefault(vehicle, {"wh": 0.0, "cost": 0.0, "count": 0})
            by_vehicle[vehicle]["wh"]    += energy_wh
            by_vehicle[vehicle]["cost"]  += cost
            by_vehicle[vehicle]["count"] += 1

    print("  " + "─" * (len(hdr) - 2))
    print(f"  {'TOTAL':>4}  {'':10}  {'':5}  {'':7}  {total_wh/1000:>6.2f}  {'':>6}  {'':>8}  {total_cost:>7.2f}")

    if by_vehicle:
        print(f"\n  By vehicle:")
        for v, d in sorted(by_vehicle.items()):
            print(f"    {v:<8}  {d['count']:>3} sessions   {d['wh']/1000:>7.2f} kWh   ${d['cost']:>7.2f}")

    print(f"\n  ~ = auto-tagged by power draw  "
          f"({VEHICLE_POWER_THRESHOLD_W/1000:.1f} kW threshold: below → Shark, above → Tesla)\n")


def cmd_tag(args):
    vehicle_map = {
        "t": "Tesla", "tesla": "Tesla",
        "s": "Shark", "shark": "Shark",
        "u": "Unknown", "unknown": "Unknown",
    }
    vehicle = vehicle_map.get(args.vehicle.lower())
    if not vehicle:
        print(f"Unknown vehicle '{args.vehicle}'. Use: tesla, shark, unknown")
        return

    conn = get_db()
    row  = conn.execute("SELECT id FROM sessions WHERE id=?", (args.session_id,)).fetchone()
    if not row:
        print(f"Session {args.session_id} not found.")
        return

    conn.execute("UPDATE sessions SET vehicle=?, auto_tagged=0 WHERE id=?", (vehicle, args.session_id))
    conn.commit()
    print(f"Session {args.session_id} → {vehicle}")


def cmd_note(args):
    conn = get_db()
    conn.execute("UPDATE sessions SET notes=? WHERE id=?", (args.note, args.session_id))
    conn.commit()
    print(f"Note saved on session {args.session_id}.")


# ── Argument parser ───────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Tesla Gen 3 Wall Connector usage tracker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Rates: General ${RATE_GENERAL}/kWh  CL2 ${RATE_CL2}/kWh  (Origin Energy QLD, post 1-Jul-2025)"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # status
    sub.add_parser("status", help="Show current charger state")

    # daemon
    d = sub.add_parser("daemon", help="Poll and log sessions")
    d.add_argument("--rate", type=float, default=DEFAULT_RATE,
                   metavar="$/KWH",
                   help=f"Electricity rate (default ${DEFAULT_RATE:.4f} General; CL2 ${RATE_CL2:.4f})")

    # report
    r = sub.add_parser("report", help="Show session history")
    grp = r.add_mutually_exclusive_group()
    grp.add_argument("--days",  type=int,  help="Last N days")
    grp.add_argument("--month", metavar="YYYY-MM", help="Specific month")
    r.add_argument("--vehicle", choices=["tesla", "shark", "unknown"],
                   help="Filter by vehicle")

    # tag
    t = sub.add_parser("tag", help="Tag session with vehicle")
    t.add_argument("session_id", type=int)
    t.add_argument("vehicle", help="tesla / shark / unknown (or t / s / u)")

    # note
    n = sub.add_parser("note", help="Add note to session")
    n.add_argument("session_id", type=int)
    n.add_argument("note")

    args = p.parse_args()

    dispatch = {
        "status": cmd_status,
        "daemon": cmd_daemon,
        "report": cmd_report,
        "tag":    cmd_tag,
        "note":   cmd_note,
    }
    dispatch[args.cmd](args)


if __name__ == "__main__":
    main()
