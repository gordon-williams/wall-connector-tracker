#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Gordon Williams — https://github.com/gordon-williams/wall-connector-tracker
"""Back up a running tracker's history by pulling its REST API into a local SQLite file.

Reads only over HTTP, so the machine holding the data needs no extra software,
no SSH access and no code changes — it works against any version of the server.
Intended to run unattended on a schedule.

    python3 wc_backup.py http://192.168.1.50:8090 --out ~/Backups/wc-history.db

Safe to run at any time, including mid-charge:

  * The new copy is assembled in a temporary file and only swapped into place
    once it has been verified, so an interrupted or failed run never damages
    the existing backup.
  * Samples for finished sessions are fetched once and reused, so routine runs
    cost one HTTP request.
  * If the source is unreachable the previous backup is left exactly as it was
    and the exit status is non-zero, so a scheduler can report the failure.

Exit status: 0 on success, 1 on any failure.
"""

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY,
    start_time  TEXT,
    end_time    TEXT,
    duration_s  INTEGER,
    energy_wh   REAL,
    vehicle     TEXT,
    auto_tagged INTEGER,
    rate_kwh    REAL,
    notes       TEXT,
    start_soc   REAL,
    end_soc     REAL,
    energy_wh_baseline REAL
);
CREATE TABLE IF NOT EXISTS session_samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL,
    ts          TEXT NOT NULL,
    energy_wh   REAL,
    current_a   REAL,
    grid_v      REAL
);
CREATE INDEX IF NOT EXISTS idx_samples_session ON session_samples (session_id);
CREATE TABLE IF NOT EXISTS backup_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
-- Records which sessions we have already pulled samples for, and the totals
-- they had at the time. Tracking the fetch rather than the resulting rows
-- matters because a brief plug-in can legitimately have zero samples, and
-- "no rows stored" would otherwise re-fetch it on every run forever.
CREATE TABLE IF NOT EXISTS sample_fetch (
    session_id INTEGER PRIMARY KEY,
    energy_wh  REAL,
    duration_s INTEGER
);
"""

SESSION_COLUMNS = ("id", "start_time", "end_time", "duration_s", "energy_wh",
                   "vehicle", "auto_tagged", "rate_kwh", "notes",
                   "start_soc", "end_soc", "energy_wh_baseline")


def fetch(base, path, timeout=30):
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=timeout) as r:
        return json.loads(r.read())


def open_target(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def backup(base: str, dest: str, verbose=True) -> int:
    dest = os.path.abspath(os.path.expanduser(dest))
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = dest + ".partial"

    def say(msg):
        if verbose:
            print(msg)

    # ── Read the source first. If it's unreachable we stop before touching
    #    anything, so the existing backup survives untouched.
    try:
        sessions = fetch(base, "/api/sessions")
        config   = fetch(base, "/api/config")
    except urllib.error.URLError as exc:
        print(f"FAILED: {base} unreachable — {exc.reason}. Existing backup untouched.")
        return 1
    except Exception as exc:
        print(f"FAILED: could not read {base} — {exc}. Existing backup untouched.")
        return 1

    if not isinstance(sessions, list):
        print("FAILED: unexpected response from /api/sessions. Existing backup untouched.")
        return 1

    # ── Start from the previous backup so finished sessions aren't re-fetched.
    #    If it can't be read, set it aside and rebuild from scratch — an
    #    unreadable file must never become a permanent block on backing up.
    if os.path.exists(tmp):
        os.remove(tmp)
    if os.path.exists(dest):
        try:
            src = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
            dst = sqlite3.connect(tmp)
            with dst:
                src.backup(dst)
            src.close()
            dst.close()
        except Exception as exc:
            if os.path.exists(tmp):
                os.remove(tmp)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            quarantine = f"{dest}.unreadable-{stamp}"
            os.rename(dest, quarantine)
            print(f"WARNING: existing backup could not be read ({exc}). "
                  f"Moved to {quarantine} and rebuilding from scratch.")
    conn = open_target(tmp)

    done = {r["session_id"]: r for r in conn.execute(
        "SELECT session_id, energy_wh, duration_s FROM sample_fetch")}

    placeholders = ",".join("?" * len(SESSION_COLUMNS))
    conn.executemany(
        f"INSERT OR REPLACE INTO sessions ({', '.join(SESSION_COLUMNS)}) "
        f"VALUES ({placeholders})",
        [tuple(s.get(c) for c in SESSION_COLUMNS) for s in sessions])
    conn.commit()

    fetched = skipped = 0
    for s in sessions:
        sid = s["id"]
        old = done.get(sid)
        stale = (
            old is None                                    # never pulled
            or not s.get("end_time")                       # still in progress
            or (old["energy_wh"] or 0) != (s.get("energy_wh") or 0)
            or (old["duration_s"] or 0) != (s.get("duration_s") or 0)
        )
        if not stale:
            skipped += 1
            continue
        try:
            rows = fetch(base, f"/api/sessions/{sid}/samples")
        except Exception as exc:
            conn.close()
            os.remove(tmp)
            print(f"FAILED: could not read samples for session {sid} — {exc}. "
                  f"Existing backup untouched.")
            return 1
        conn.execute("DELETE FROM session_samples WHERE session_id=?", (sid,))
        conn.executemany(
            "INSERT INTO session_samples (session_id, ts, energy_wh, current_a, grid_v) "
            "VALUES (?,?,?,?,?)",
            [(sid, r.get("ts"), r.get("energy_wh"), r.get("current_a"), r.get("grid_v"))
             for r in rows])
        conn.execute(
            "INSERT OR REPLACE INTO sample_fetch (session_id, energy_wh, duration_s) "
            "VALUES (?,?,?)", (sid, s.get("energy_wh"), s.get("duration_s")))
        fetched += 1
    conn.commit()

    now = datetime.now(timezone.utc).isoformat()
    for k, v in (("source", base), ("taken_at", now),
                 ("source_sessions", str(len(sessions))),
                 ("config", json.dumps(config))):
        conn.execute("INSERT OR REPLACE INTO backup_meta (key, value) VALUES (?,?)", (k, v))
    conn.commit()

    # ── Verify before letting this replace a known-good backup
    problems = []
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        problems.append("integrity_check failed")
    n_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    if n_sessions != len(sessions):
        problems.append(f"session count {n_sessions} != source {len(sessions)}")
    n_samples = conn.execute("SELECT COUNT(*) FROM session_samples").fetchone()[0]
    # A brief plug-in that never drew current has no samples at source; only
    # a substantial session missing its trend data is worth reporting.
    empty = conn.execute(
        "SELECT COUNT(*) FROM sessions s WHERE (s.energy_wh or 0) > 100 AND NOT EXISTS "
        "(SELECT 1 FROM session_samples x WHERE x.session_id = s.id)").fetchone()[0]
    conn.close()
    if problems:
        os.remove(tmp)
        print("FAILED: " + "; ".join(problems) + ". Existing backup untouched.")
        return 1

    os.replace(tmp, dest)   # atomic — a sync client never sees a partial file
    size = os.path.getsize(dest) / 1e6
    say(f"{now[:19]}Z  {n_sessions} sessions, {n_samples} samples -> {dest} ({size:.2f} MB)"
        f"  [fetched {fetched}, reused {skipped}"
        + (f"]  WARNING: {empty} session(s) over 0.1 kWh have no trend data"
           if empty else "]"))
    return 0


def main():
    p = argparse.ArgumentParser(description="Back up a tracker's history over HTTP")
    p.add_argument("base", nargs="?", default="http://localhost:8090",
                   help="Base URL of the server to back up")
    p.add_argument("--out", required=True, metavar="FILE",
                   help="Destination SQLite file")
    p.add_argument("--quiet", action="store_true",
                   help="Only print on failure (for scheduled runs)")
    args = p.parse_args()
    sys.exit(backup(args.base, args.out, verbose=not args.quiet))


if __name__ == "__main__":
    main()
