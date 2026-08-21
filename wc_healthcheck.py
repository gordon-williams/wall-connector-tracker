#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Gordon Williams — https://github.com/gordon-williams/wall-connector-tracker
"""Snapshot a running tracker over HTTP, and compare snapshots across a deploy.

Run it before an upgrade, run it again afterwards, and it will tell you whether
anything was lost. It only reads the REST API, so it works against a server on
another machine and needs no access to that machine's filesystem.

    python3 wc_healthcheck.py http://192.168.1.50:8090 --save before.json
    # ... deploy ...
    python3 wc_healthcheck.py http://192.168.1.50:8090 --compare before.json

Exit status is 0 when healthy (or when a comparison shows no loss), 1 otherwise,
so it can gate a scripted deploy.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone


def fetch(base, path, timeout=20):
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=timeout) as r:
        return json.loads(r.read())


def snapshot(base: str) -> dict:
    """Everything we can learn about a server without touching its disk."""
    sessions = fetch(base, "/api/sessions")
    summary  = fetch(base, "/api/summary")
    config   = fetch(base, "/api/config")
    try:
        status = fetch(base, "/api/status")
    except Exception:
        status = {}

    # The dashboard carries a build marker: the MIRROR constant only exists in
    # the templated version of the page.
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/", timeout=20) as r:
            html = r.read().decode("utf-8", "replace")
        build = "templated" if "const MIRROR" in html else "legacy"
    except Exception:
        build = "unknown"

    starts = sorted(s["start_time"] for s in sessions if s.get("start_time"))
    totals = summary.get("totals") or {}
    return {
        "taken_at":      datetime.now(timezone.utc).isoformat(),
        "base":          base,
        "build":         build,
        "reachable":     True,
        "polling_ok":    bool(status.get("ok")),
        "last_poll":     status.get("last_poll"),
        "session_count": len(sessions),
        "max_id":        max((s["id"] for s in sessions), default=0),
        "first_start":   starts[0] if starts else None,
        "last_start":    starts[-1] if starts else None,
        "total_wh":      totals.get("total_wh") or 0,
        "total_cost":    totals.get("total_cost") or 0,
        "by_vehicle":    {v["vehicle"]: v["count"] for v in summary.get("by_vehicle", [])},
        "vehicles_cfg":  [v.get("name") for v in config.get("vehicles", [])],
        "rates":         {k: config.get(k) for k in
                          ("rate_general_kwh", "rate_ev_powerup_kwh",
                           "offpeak_start_hour", "offpeak_end_hour")},
        # Session ids are the ground truth for "did we lose anything"
        "ids":           sorted(s["id"] for s in sessions),
    }


def show(s: dict):
    print(f"  server        : {s['base']}  ({s['build']} build)")
    print(f"  polling       : {'ok' if s['polling_ok'] else 'NOT POLLING'}"
          f"   last poll {s['last_poll'] or '—'}")
    print(f"  sessions      : {s['session_count']}  (max id {s['max_id']})")
    print(f"  range         : {(s['first_start'] or '—')[:10]} → {(s['last_start'] or '—')[:10]}")
    print(f"  energy / cost : {s['total_wh']/1000:.1f} kWh  ${s['total_cost']:.2f}")
    print(f"  by vehicle    : " + ", ".join(f"{k} {v}" for k, v in sorted(s["by_vehicle"].items())))
    print(f"  configured    : {', '.join(s['vehicles_cfg'])}")
    r = s["rates"]
    print(f"  rates         : general ${r['rate_general_kwh']}/kWh, "
          f"EV ${r['rate_ev_powerup_kwh']}/kWh, "
          f"off-peak {r['offpeak_start_hour']}:00–{r['offpeak_end_hour']}:00")


def compare(before: dict, after: dict) -> int:
    print("\nComparison")
    problems, notes = [], []

    lost = sorted(set(before["ids"]) - set(after["ids"]))
    if lost:
        problems.append(f"{len(lost)} session(s) MISSING after the change: {lost[:10]}"
                        + (" …" if len(lost) > 10 else ""))
    gained = sorted(set(after["ids"]) - set(before["ids"]))
    if gained:
        notes.append(f"{len(gained)} new session(s) recorded since the snapshot: {gained}")

    d_wh = after["total_wh"] - before["total_wh"]
    if d_wh < -0.5:
        problems.append(f"total energy dropped by {abs(d_wh)/1000:.2f} kWh")
    elif d_wh > 0.5:
        notes.append(f"total energy grew by {d_wh/1000:.2f} kWh")

    if not after["polling_ok"]:
        problems.append("server is not polling the charger")

    for key, label in (("vehicles_cfg", "configured vehicles"), ("rates", "rates")):
        if before[key] != after[key]:
            problems.append(f"{label} changed: {before[key]} → {after[key]}")

    if before["build"] != after["build"]:
        notes.append(f"build changed: {before['build']} → {after['build']}")

    for n in notes:
        print(f"  note    {n}")
    for p in problems:
        print(f"  PROBLEM {p}")
    if not problems:
        print("  ✓ no data lost, config unchanged, server healthy")
    return 1 if problems else 0


def main():
    p = argparse.ArgumentParser(description="Snapshot and compare a tracker over HTTP")
    p.add_argument("base", nargs="?", default="http://localhost:8090",
                   help="Base URL of the server (default http://localhost:8090)")
    p.add_argument("--save",    metavar="FILE", help="Write the snapshot to FILE")
    p.add_argument("--compare", metavar="FILE", help="Compare against a saved snapshot")
    args = p.parse_args()

    try:
        snap = snapshot(args.base)
    except urllib.error.URLError as exc:
        print(f"UNREACHABLE: {args.base} — {exc.reason}")
        sys.exit(1)
    except Exception as exc:
        print(f"FAILED to read {args.base}: {exc}")
        sys.exit(1)

    print("Snapshot")
    show(snap)

    if args.save:
        with open(args.save, "w") as f:
            json.dump(snap, f, indent=2)
        print(f"\nSaved to {args.save}")

    if args.compare:
        with open(args.compare) as f:
            before = json.load(f)
        print("\nBefore")
        show(before)
        sys.exit(compare(before, snap))

    sys.exit(0 if snap["polling_ok"] else 1)


if __name__ == "__main__":
    main()
