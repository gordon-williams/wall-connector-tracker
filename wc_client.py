#!/usr/bin/env python3
"""Wall Connector CLI client — talks to wc_server.py over HTTP.

Usage:
    python3 wc_client.py status
    python3 wc_client.py report [--days N] [--month YYYY-MM] [--vehicle tesla|shark]
    python3 wc_client.py tag <session_id> <tesla|shark|unknown>
    python3 wc_client.py note <session_id> <text>

Set WC_SERVER env var to override default (http://localhost:8090):
    WC_SERVER=http://192.168.86.10:8090 python3 wc_client.py status
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime

SERVER = os.environ.get("WC_SERVER", "http://localhost:8090").rstrip("/")


def api_get(path):
    try:
        with urllib.request.urlopen(SERVER + path, timeout=8) as r:
            return json.loads(r.read())
    except urllib.error.URLError as e:
        print(f"ERROR: Cannot reach server at {SERVER}  ({e})")
        sys.exit(1)


def api_patch(path, payload):
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(SERVER + path, data=data, method="PATCH",
                                   headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except urllib.error.URLError as e:
        print(f"ERROR: {e}")
        sys.exit(1)


def fmt_dur(s):
    if not s:
        return "–"
    h, rem = divmod(int(s), 3600)
    m = rem // 60
    return f"{h}h {m:02d}m" if h else f"{m}m"


# ── status ────────────────────────────────────────────────────────────────────

def cmd_status(_args):
    d  = api_get("/api/status")
    v  = d.get("vitals") or {}
    lt = d.get("lifetime") or {}
    cs = d.get("current_session")

    ok = d.get("ok", False)
    poll = d.get("last_poll", "–")
    try:
        poll = datetime.fromisoformat(poll).astimezone().strftime("%H:%M:%S")
    except Exception:
        pass

    state = "CHARGING" if v.get("contactor_closed") else \
            "CONNECTED" if v.get("vehicle_connected") else "IDLE"

    print(f"\n  Wall Connector  —  {poll}  [{state}]")
    print(f"  {'Server':22s} {SERVER}  ({'OK' if ok else 'ERROR'})")
    if v:
        print(f"  {'Grid':22s} {v.get('grid_v', 0):.1f}V / {v.get('grid_hz', 0):.2f}Hz")
        print(f"  {'MCU temp':22s} {v.get('mcu_temp_c', 0):.1f}°C  "
              f"handle {v.get('handle_temp_c', 0):.1f}°C")

    if cs:
        wh  = cs.get("energy_wh") or 0
        dur = cs.get("duration_s") or 0
        avg = wh / (dur / 3600) if dur > 10 else 0
        print(f"\n  Active session #{cs['id']}  [{cs['vehicle']}]")
        print(f"  {'Energy':22s} {wh/1000:.2f} kWh")
        print(f"  {'Duration':22s} {fmt_dur(dur)}")
        if avg:
            print(f"  {'Avg power':22s} {avg:.0f} W")
        print(f"  {'Est. cost':22s} ${cs['cost']:.2f}")

    if lt:
        print(f"\n  Lifetime")
        print(f"  {'Sessions':22s} {lt.get('charge_starts', 0):,}")
        print(f"  {'Energy':22s} {lt.get('energy_wh', 0)/1000:.1f} kWh")
        print(f"  {'Charging time':22s} {lt.get('charging_time_s', 0)//3600:,} hrs")
    print()


# ── report ────────────────────────────────────────────────────────────────────

def cmd_report(args):
    params = []
    if args.days:    params.append(f"days={args.days}")
    if args.month:   params.append(f"month={args.month}")
    if args.vehicle: params.append(f"vehicle={args.vehicle}")
    qs = ("?" + "&".join(params)) if params else ""

    rows = api_get(f"/api/sessions{qs}")

    if not rows:
        print("No sessions found.")
        return

    total_wh = 0.0
    total_cost = 0.0
    by_vehicle: dict[str, dict] = {}

    hdr = f"  {'ID':>4}  {'Date':10}  {'Time':5}  {'Vehicle':8}  {'kWh':>6}  {'Avg W':>6}  {'Duration':>8}  {'Rate':>8}  {'Cost':>7}  Notes"
    print(f"\n{hdr}")
    print("  " + "─" * (len(hdr) - 2))

    for r in rows:
        wh  = r.get("energy_wh") or 0
        dur = r.get("duration_s") or 0
        avg = int(wh / (dur / 3600)) if (wh and dur > 10) else None
        vehicle = r.get("vehicle") or "Unknown"
        auto    = "~" if r.get("auto_tagged") else " "
        rate    = r.get("rate_kwh") or 0
        cost    = r.get("cost") or 0
        notes   = r.get("notes") or ""

        try:
            dt = datetime.fromisoformat(r["start_time"]).astimezone()
            date_str = dt.strftime("%Y-%m-%d")
            time_str = dt.strftime("%H:%M")
        except Exception:
            date_str = (r.get("start_time") or "")[:10]
            time_str = (r.get("start_time") or "")[11:16]

        print(f"  {r['id']:>4}  {date_str}  {time_str}  {auto}{vehicle:<7}  "
              f"{wh/1000:>6.2f}  {str(avg) if avg else '–':>6}  "
              f"{fmt_dur(dur):>8}  ${rate:.4f}  {cost:>7.2f}  {notes}")

        if wh:
            total_wh += wh
            total_cost += cost
            by_vehicle.setdefault(vehicle, {"wh": 0.0, "cost": 0.0, "count": 0})
            by_vehicle[vehicle]["wh"]    += wh
            by_vehicle[vehicle]["cost"]  += cost
            by_vehicle[vehicle]["count"] += 1

    print("  " + "─" * (len(hdr) - 2))
    print(f"  {'':>4}  {'':10}  {'':5}  {'TOTAL':8}  "
          f"{total_wh/1000:>6.2f}  {'':>6}  {'':>8}  {'':>8}  {total_cost:>7.2f}")

    if by_vehicle:
        print(f"\n  By vehicle:")
        for v, d in sorted(by_vehicle.items()):
            print(f"    {v:<10} {d['count']:>3} sessions  {d['wh']/1000:>7.2f} kWh  ${d['cost']:>7.2f}")

    print(f"\n  ~ = auto-tagged by power draw\n")


# ── tag / note ────────────────────────────────────────────────────────────────

def cmd_tag(args):
    vehicle_map = {
        "t": "Tesla", "tesla": "Tesla",
        "s": "Shark", "shark": "Shark",
        "u": "Unknown", "unknown": "Unknown",
    }
    vehicle = vehicle_map.get(args.vehicle.lower())
    if not vehicle:
        print(f"Unknown vehicle '{args.vehicle}'. Use: tesla / shark / unknown (or t / s / u)")
        return
    r = api_patch(f"/api/sessions/{args.session_id}", {"vehicle": vehicle})
    print(f"Session {args.session_id} → {vehicle}  (rate: ${r['rate_kwh']:.4f}/kWh)")


def cmd_note(args):
    api_patch(f"/api/sessions/{args.session_id}", {"notes": args.note})
    print(f"Note saved on session {args.session_id}.")


# ── Argument parser ───────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description=f"Wall Connector CLI client  [{SERVER}]",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Show current charger state")

    r = sub.add_parser("report", help="Session history")
    grp = r.add_mutually_exclusive_group()
    grp.add_argument("--days",  type=int,      help="Last N days")
    grp.add_argument("--month", metavar="YYYY-MM")
    r.add_argument("--vehicle", choices=["tesla", "shark", "unknown"])

    t = sub.add_parser("tag", help="Tag session with vehicle")
    t.add_argument("session_id", type=int)
    t.add_argument("vehicle", help="tesla / shark / unknown  (or t / s / u)")

    n = sub.add_parser("note", help="Add note to session")
    n.add_argument("session_id", type=int)
    n.add_argument("note")

    args = p.parse_args()
    {"status": cmd_status, "report": cmd_report,
     "tag": cmd_tag, "note": cmd_note}[args.cmd](args)


if __name__ == "__main__":
    main()
