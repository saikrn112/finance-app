#!/usr/bin/env bash
# Refresh the desktop app's database from the container ("webapp") one.
#
# ## Why this exists
#
# While the macOS app is in beta the container app remains the source of truth: Plaid syncs,
# imports and project edits all happen there. The desktop app keeps its own copy in Application
# Support, so the two drift -- and they drift in *both* directions, invisibly. Measured once:
# the app's copy had 3 transactions the container's did not, while the container's had 30 more
# project links, 306 splits and 22 notes the app's did not. Nothing warned about either.
#
# So this script exists to make one direction explicit and repeatable:
#
#     container database  --->  desktop app database
#
# It is deliberately NOT a merge. Two SQLite databases that have both been written cannot be
# reconciled without inventing an answer for every differing row, and every one of those rows is
# real financial history. Instead the container's copy replaces the app's, and anything the app
# alone knows about is carried across (see "app-only tables" below).
#
# Run `--check` freely; it only reads. Run without it to actually replace the database.
#
# ## What is lost
#
# Anything written *only* in the desktop app since the last refresh -- most plausibly a Plaid
# sync it ran itself. Plaid re-fetches on the next sync, so this is recoverable, but it is a real
# overwrite and the script prints the delta before doing it.
#
# ## After cutover
#
# When the desktop app becomes the source of truth, stop running this: from that point on it
# would overwrite the newer database with the older one. Delete the script, or move the app to a
# shared data folder (Diagnostics > Database > Change...) so there is only one copy at all.

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

APP_SUPPORT="$HOME/Library/Application Support/$APP_NAME"
APP_DATA="$APP_SUPPORT/data/runtime/prod"
APP_DB="$APP_DATA/finances.db"

# NOT $REPO_ROOT. This script lives on a branch that is normally checked out as a git *worktree*,
# and a worktree has its own untracked, empty `data/`. Using $REPO_ROOT read the container's
# database as zero rows of everything and would have replaced five years of history with an empty
# database that passes integrity_check. So resolve the container's checkout explicitly: the main
# worktree, which is where the container app is actually run from.
if [ -n "${FINANCE_WEBAPP_ROOT:-}" ]; then
    SRC_ROOT="$FINANCE_WEBAPP_ROOT"
else
    SRC_ROOT="$(git -C "$REPO_ROOT" worktree list --porcelain | awk '/^worktree /{print $2; exit}')"
fi
[ -n "$SRC_ROOT" ] || die "could not resolve the container's checkout; set FINANCE_WEBAPP_ROOT"
SRC_DATA="$SRC_ROOT/data/runtime/prod"
SRC_DB="$SRC_DATA/finances.db"

CHECK_ONLY=0
RELAUNCH=1
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --check)      CHECK_ONLY=1 ;;
        --no-launch)  RELAUNCH=0 ;;
        --force)      FORCE=1 ;;
        -h|--help)
            sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            echo
            echo "usage: $(basename "$0") [--check] [--no-launch] [--force]"
            echo "  --check      compare the two databases and change nothing"
            echo "  --no-launch  refresh but do not start the app afterwards"
            echo "  --force      refresh even though the container's copy has fewer transactions"
            echo
            echo "env: FINANCE_WEBAPP_ROOT  the container app's checkout (default: the main worktree)"
            exit 0 ;;
        *) die "unknown option: $arg" ;;
    esac
done

[ -f "$SRC_DB" ] || die "no container database at $SRC_DB"
log "source: $SRC_DB"

# Counts, not amounts. This output is read in terminals and pasted into chats, and the tables
# below are exactly the ones whose absence was mistaken for a bug in the app.
counts() {
    local db="$1"
    python3 - "$db" <<'PY'
import sqlite3, sys
db = sys.argv[1]
try:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
except sqlite3.Error as exc:
    print(f"unreadable: {exc}"); raise SystemExit(1)
have = {r[0] for r in c.execute("select name from sqlite_master where type='table'")}
out = []
for table in ("transactions", "projects", "transaction_projects", "transaction_splits", "feedback"):
    n = c.execute(f"select count(*) from {table}").fetchone()[0] if table in have else "-"
    out.append(f"{table}={n}")
if "transactions" in have:
    n = c.execute("select count(*) from transactions where notes is not null and notes != ''").fetchone()[0]
    out.append(f"txn_notes={n}")
    latest = c.execute("select max(date) from transactions").fetchone()[0]
    out.append(f"latest={latest}")
print("  " + "  ".join(str(x) for x in out))
PY
}

log "container (source of truth)"
counts "$SRC_DB"
if [ -f "$APP_DB" ]; then
    log "desktop app"
    counts "$APP_DB"
else
    warn "the desktop app has no database yet -- this will create it"
fi

if [ "$CHECK_ONLY" = 1 ]; then
    log "--check: nothing was written"
    exit 0
fi

# A refresh that shrinks the history is the signature of pointing at the wrong database -- an empty
# `data/` in a worktree, an unmounted volume, a fresh demo-mode copy. Every one of those produces a
# perfectly valid SQLite file, so integrity_check cannot catch it and neither can a backup that is
# only discovered to be needed afterwards. This is the one check that would have.
txn_count() {
    [ -f "$1" ] || { echo 0; return; }
    sqlite3 "file:$1?mode=ro" \
        "select coalesce((select count(*) from transactions), 0)" 2>/dev/null || echo 0
}
SRC_TXNS="$(txn_count "$SRC_DB")"
APP_TXNS="$(txn_count "$APP_DB")"

if [ "$SRC_TXNS" -eq 0 ]; then
    if [ "$APP_TXNS" -gt 0 ]; then
        stake="refusing to overwrite the ${APP_TXNS} the app has"
    else
        stake="refusing to install it"
    fi
    die "the container database has no transactions -- $stake.
       Is $SRC_ROOT the checkout the container app actually runs from?
       Set FINANCE_WEBAPP_ROOT if not."
fi
if [ "$SRC_TXNS" -lt "$APP_TXNS" ] && [ "$FORCE" = 0 ]; then
    die "the container has fewer transactions than the app ($SRC_TXNS < $APP_TXNS).
       That usually means the wrong source database. Re-run with --force if it is genuinely correct."
fi

# The app must not be running. Replacing a database file under an open SQLite connection is how
# you get a corrupt one, and the backend holds it open for the app's whole lifetime.
app_pids() { pgrep -f "$APP_NAME.app/Contents/MacOS/$APP_NAME" || true; }
backend_pids() { pgrep -f "backend/bootstrap.py" || true; }

if [ -n "$(app_pids)$(backend_pids)" ]; then
    log "quitting the app so nothing holds the database open"
    osascript -e "tell application \"$APP_NAME\" to quit" >/dev/null 2>&1 || true
    for _ in $(seq 1 20); do
        [ -z "$(app_pids)$(backend_pids)" ] && break
        sleep 0.5
    done
    [ -n "$(app_pids)$(backend_pids)" ] && die "the app is still running; quit it and re-run"
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$APP_DATA"

# A backup of what is about to be overwritten, before anything else touches it.
BACKUP="(none -- the app had no database to overwrite)"
if [ -f "$APP_DB" ]; then
    BACKUP="/tmp/finances.app-before-refresh-$STAMP.db"
    cp "$APP_DB" "$BACKUP"
    chmod 600 "$BACKUP"
    log "backed up the app's current database -> $BACKUP"
fi

# Tables the app has and the container does not. Today that is the feedback pair, which exists
# only in the desktop build -- but deriving the list instead of hard-coding it means a table added
# to the app later is carried across rather than silently destroyed on the next refresh.
CARRIED="/tmp/finance-app-only-tables-$STAMP.sql"
if [ -f "$APP_DB" ]; then
    python3 - "$APP_DB" "$SRC_DB" "$CARRIED" <<'PY'
import sqlite3, sys
app_db, src_db, out = sys.argv[1:4]
app = sqlite3.connect(f"file:{app_db}?mode=ro", uri=True)
src = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)

def tables(conn):
    return {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}

only = sorted(tables(app) - tables(src))
lines, rows = [], 0
for name in only:
    for stmt, in app.execute(
        "select sql from sqlite_master where tbl_name=? and sql is not null", (name,)
    ):
        lines.append(f"{stmt};")
    cols = [d[1] for d in app.execute(f"pragma table_info({name})")]
    for values in app.execute(f"select {','.join(cols)} from {name}"):
        placeholders = ",".join(
            "NULL" if v is None else
            str(v) if isinstance(v, (int, float)) else
            "'" + str(v).replace("'", "''") + "'"
            for v in values
        )
        lines.append(f"INSERT OR REPLACE INTO {name} ({','.join(cols)}) VALUES ({placeholders});")
        rows += 1

# app_metadata exists in both, so the loop above skips it -- but the feedback numbering counter
# lives there and resetting it would make deleted note numbers get reused.
if "app_metadata" in tables(app) and "app_metadata" in tables(src):
    for key, value in app.execute(
        "select key, value from app_metadata where key like 'feedback%'"
    ):
        v = "NULL" if value is None else "'" + str(value).replace("'", "''") + "'"
        lines.append(
            f"INSERT OR REPLACE INTO app_metadata (key, value) VALUES "
            f"('{key.replace(chr(39), chr(39) * 2)}', {v});"
        )
        rows += 1

open(out, "w").write("\n".join(lines) + ("\n" if lines else ""))
print(f"  app-only tables: {', '.join(only) if only else 'none'} ({rows} rows carried)")
PY
else
    : > "$CARRIED"
fi

# SQLite's online backup API, not cp: the container app is usually running, and a plain file copy
# can catch a half-written transaction. .backup takes a consistent snapshot of a live database.
SNAPSHOT="/tmp/finances.snapshot-$STAMP.db"
log "snapshotting the container database"
sqlite3 "$SRC_DB" ".backup $SNAPSHOT"
sqlite3 "$SNAPSHOT" "pragma integrity_check" | grep -qx ok || die "the snapshot is corrupt; nothing was replaced"

log "installing the snapshot"
mv "$SNAPSHOT" "$APP_DB"
chmod 600 "$APP_DB"
# Any stale journal belongs to the replaced database, not this one.
rm -f "$APP_DB-wal" "$APP_DB-shm" "$APP_DB-journal"

if [ -s "$CARRIED" ]; then
    log "restoring the app-only tables"
    sqlite3 "$APP_DB" < "$CARRIED" || die "could not restore app-only data; the backup is at $BACKUP"
fi

# The vault metadata identifies which backup this history is, and .oauth holds the refresh token.
# Carrying the database without them leaves the app claiming a vault it cannot reach.
for item in vault_metadata.json .oauth; do
    if [ -e "$SRC_DATA/$item" ]; then
        rm -rf "$APP_DATA/$item"
        cp -R "$SRC_DATA/$item" "$APP_DATA/$item"
        log "copied $item"
    fi
done

sqlite3 "$APP_DB" "pragma integrity_check" | grep -qx ok \
    || die "the installed database fails integrity_check; restore from $BACKUP"

log "desktop app after the refresh"
counts "$APP_DB"
rm -f "$CARRIED"

if [ "$RELAUNCH" = 1 ]; then
    BUNDLE="$BUILD_DIR/$APP_NAME.app"
    if [ -d "$BUNDLE" ]; then
        log "relaunching"
        open "$BUNDLE"
    else
        warn "no bundle at $BUNDLE -- build it, then launch the app yourself"
    fi
fi
