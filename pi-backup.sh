#!/bin/sh
# Wall Connector history backup to cloud storage via rclone.
#
# Snapshot the live database, VERIFY it, and only then upload.
#
# The verification is the whole point. sqlite3 creates a new empty database
# when handed a path that doesn't exist, so a missing or renamed database
# produces a successful-looking 4 KB snapshot which then replaces a good
# backup with nothing — silently, because every command returned 0. Checking
# the snapshot before uploading is what stops a backup system from destroying
# the thing it is meant to protect.
#
# Run from cron. Nothing is written to the log unless something goes wrong, so
# an empty log means every run succeeded. FAILED means the database was not
# backed up; WARNING means it was, but config.json was not.
#
# Absolute paths throughout because cron runs with a minimal PATH, and rclone is
# given an explicit --config because cron does not guarantee HOME.

set -u

DB=/home/pi/wallconnector/wc_sessions.db
CONFIG=/home/pi/wallconnector/config.json
TMP=/tmp/wc-history.db
LOG=/home/pi/wallconnector/backup.log
REMOTE=dropbox:Charging/
RCLONE_CONFIG=/home/pi/.config/rclone/rclone.conf

# Refuse to upload a backup smaller than this. Sessions only ever accumulate,
# so a snapshot below the floor means something is wrong with the source, not
# that history shrank. Raise it over time if you like; never lower it to make
# a failing backup pass.
MIN_SESSIONS=50

SQLITE=/usr/bin/sqlite3
RCLONE=/usr/bin/rclone
PYTHON=/usr/bin/python3

# The snapshot inherits WAL mode from the source, so verifying it (which means
# opening it) creates -wal and -shm beside it. They must never linger: a
# sidecar that reaches the backup location can be picked up by a later restore
# and applied to a database it does not belong to.
cleanup() {
    rm -f "$TMP" "$TMP-wal" "$TMP-shm"
}

fail() {
    echo "$(date -Is) backup FAILED: $1" >> "$LOG"
    cleanup
    exit 1
}

# config.json is worth having but is not the irreplaceable part. A problem
# with it must never stop the database from being backed up, so it warns
# rather than failing the run.
warn() {
    echo "$(date -Is) backup WARNING: $1" >> "$LOG"
}

[ -f "$DB" ] || fail "database missing at $DB"
[ -x "$SQLITE" ] || fail "sqlite3 not found at $SQLITE"
[ -x "$RCLONE" ] || fail "rclone not found at $RCLONE"
[ -f "$RCLONE_CONFIG" ] || fail "rclone config missing at $RCLONE_CONFIG"

cleanup

# Consistent snapshot via SQLite's backup API — safe while the server is
# writing, and correct in WAL mode where recent commits live in the sidecar.
"$SQLITE" "$DB" ".backup '$TMP'" || fail "snapshot command failed"
[ -s "$TMP" ] || fail "snapshot produced no file"

integrity=$("$SQLITE" "$TMP" "pragma integrity_check;" 2>&1) \
    || fail "integrity check could not run: $integrity"
[ "$integrity" = "ok" ] || fail "integrity check returned: $integrity"

count=$("$SQLITE" "$TMP" "select count(*) from sessions;" 2>&1) \
    || fail "session count could not run: $count"
case "$count" in
    ''|*[!0-9]*) fail "session count was not a number: $count" ;;
esac
[ "$count" -ge "$MIN_SESSIONS" ] \
    || fail "snapshot holds only $count sessions, expected at least $MIN_SESSIONS"

"$RCLONE" --config "$RCLONE_CONFIG" copy "$TMP" "$REMOTE" >/dev/null 2>&1 \
    || fail "rclone upload failed"

cleanup

# ── config.json: rates, off-peak window and vehicle definitions ──────────────
# Small, static, and tedious to reconstruct from memory after a total loss.
# Validated before upload for the same reason the database is: a truncated
# file must not replace a good copy.
if [ ! -f "$CONFIG" ]; then
    warn "config.json missing at $CONFIG — database backed up, config was not"
elif ! "$PYTHON" -m json.tool "$CONFIG" >/dev/null 2>&1; then
    warn "config.json is not valid JSON — database backed up, config was not"
elif ! "$RCLONE" --config "$RCLONE_CONFIG" copy "$CONFIG" "$REMOTE" >/dev/null 2>&1; then
    warn "config.json upload failed — database backed up, config was not"
fi

exit 0
