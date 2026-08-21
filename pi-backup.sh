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
# Run from cron. Only failures are written to the log, so an empty log means
# every run has succeeded. Absolute paths throughout because cron runs with a
# minimal PATH, and rclone is given an explicit --config because cron does not
# guarantee HOME.

set -u

DB=/home/pi/wallconnector/wc_sessions.db
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

fail() {
    echo "$(date -Is) backup FAILED: $1" >> "$LOG"
    rm -f "$TMP"
    exit 1
}

[ -f "$DB" ] || fail "database missing at $DB"
[ -x "$SQLITE" ] || fail "sqlite3 not found at $SQLITE"
[ -x "$RCLONE" ] || fail "rclone not found at $RCLONE"
[ -f "$RCLONE_CONFIG" ] || fail "rclone config missing at $RCLONE_CONFIG"

rm -f "$TMP"

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

rm -f "$TMP"
exit 0
