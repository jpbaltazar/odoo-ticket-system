#!/bin/sh
# Back up an odoo-tickets data directory.
#
#   ./backup.sh /var/backups/odoo-tickets
#
# Three things need to survive, and they fail differently:
#   tickets.db  - the tickets themselves
#   secret.key  - the pepper. LOSE IT and every issued API key stops working,
#                 with no way to recover them; LEAK IT and keys are forgeable.
#   blobs/      - screenshots. Bulky, and the only part `otk purge` reclaims.
set -eu

DATA="${OTK_DATA_DIR:-/var/lib/odoo-tickets}"
DEST="${1:?usage: backup.sh <destination-dir>}"
KEEP="${KEEP_DAYS:-30}"
PYTHON="${PYTHON:-/opt/odoo-tickets/.venv/bin/python}"

[ -d "$DATA" ] || { echo "no data dir at $DATA" >&2; exit 1; }

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$DEST/$STAMP"
mkdir -p "$OUT"
# Backups contain every client's screenshots and the pepper.
chmod 700 "$DEST" "$OUT"

# SQLite's online backup API, not cp: the database is live and in WAL mode, so
# a plain copy can capture a torn write and restore to a corrupt file.
"$PYTHON" - "$DATA/tickets.db" "$OUT/tickets.db" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
target = sqlite3.connect(dst)
with target:
    source.backup(target)
source.close(); target.close()
PY

cp -a "$DATA/secret.key" "$OUT/secret.key"
tar -C "$DATA" -czf "$OUT/blobs.tar.gz" blobs
chmod 600 "$OUT"/*

printf 'backed up to %s (%s)\n' "$OUT" "$(du -sh "$OUT" | cut -f1)"

# Prune old snapshots.
find "$DEST" -maxdepth 1 -type d -name '20*' -mtime "+$KEEP" -exec rm -rf {} +

# Restore:
#   systemctl stop otk-api otk-web
#   cp tickets.db secret.key $OTK_DATA_DIR/
#   tar -C $OTK_DATA_DIR -xzf blobs.tar.gz
#   chown -R otk:otk $OTK_DATA_DIR && systemctl start otk-api otk-web
