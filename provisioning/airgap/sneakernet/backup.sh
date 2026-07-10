#!/usr/bin/env bash
# Sneakernet backup — export ha-2's data-of-record to a removable medium (ADR-0033 Phase 4, B2).
# The air-gap dictator's data can only leave via a controlled, offline, AUDITABLE USB round-trip (never
# via the dictator reaching the internet). Consistent sqlite snapshots (.backup) + a sha256 manifest so
# the far side can verify integrity before trusting it. Idempotent per run (timestamped dir).
#
#   backup.sh <dest-dir>            # e.g. a mounted USB:  backup.sh /media/HA-BACKUP
set -euo pipefail
DEST="${1:?usage: backup.sh <dest-dir> (e.g. a mounted USB)}"
ROOT="${HA_ROOT:-$HOME/home_automation}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ 2>/dev/null || date +%s)"
OUT="$DEST/ha2-backup-$STAMP"
mkdir -p "$OUT/db"
echo "== sneakernet backup -> $OUT =="

# 1. consistent sqlite snapshots (.backup is atomic vs live writers)
for db in hot weather control rungs quarantine; do
  SRC="$ROOT/instance/db/$db.db"
  [ -f "$SRC" ] || continue
  sqlite3 "$SRC" ".backup '$OUT/db/$db.db'" && echo "  db/$db.db"
done

# 2. parquet archive + config-of-record
[ -d "$ROOT/instance/db/parquet" ] && { rsync -a "$ROOT/instance/db/parquet" "$OUT/db/"; echo "  db/parquet/"; }
for f in instance/devices.yaml instance/weather.env instance/control.yaml; do
  [ -f "$ROOT/$f" ] && { install -D -m 0644 "$ROOT/$f" "$OUT/$f"; echo "  $f"; }
done

# 3. integrity manifest (sha256 of every file)
( cd "$OUT" && find . -type f ! -name MANIFEST.sha256 -print0 | sort -z | xargs -0 sha256sum > MANIFEST.sha256 )
NFILES=$(grep -c . "$OUT/MANIFEST.sha256"); SIZE=$(du -sh "$OUT" | cut -f1)
echo "== manifest: $NFILES files, $SIZE =="

# 4. record the backup so the nag knows it happened
date -u +%Y-%m-%dT%H:%M:%SZ > "$ROOT/instance/.last-sneakernet-backup" 2>/dev/null || true
echo "OK: backup complete at $OUT  (verify on the far side: verify-restore.sh $OUT)"
