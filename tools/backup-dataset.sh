#!/usr/bin/env bash
# backup-dataset.sh — off-box, consistent snapshot of the live home-automation dataset.
#
# WHY: relocate/restamp (server/maintenance/device_relocate.py) rewrites readings.area across
# hot.db + the parquet archive. device_relocate takes a per-op backup, but only 210-LOCAL
# (instance/db/backups/) and hot.db is NOT in the standby sync — so before we trust the
# UI-driven data-movement architecture we take a full, OFF-BOX baseline we can roll back to.
#
# WHAT: a WAL-safe sqlite `.backup` of every operational DB + the parquet archive + the
# relocate-relevant registry/geometry config (NO secrets), bundled with a sha256 manifest and
# the code git HEAD, pushed to the 245 backup share (//245/backup, mounted at ~/Desktop/Backup)
# and also retained on 210. Reversible/read-only wrt production — it only reads + copies.
#
# USAGE:  tools/backup-dataset.sh [label]
#   e.g.  tools/backup-dataset.sh pre-relocate-trial
# Restore: docs/runbook-dataset-restore.md
set -euo pipefail

HOST="${HA_DATASET_HOST:-192.168.0.210}"
RREPO="${HA_DATASET_REPO:-home_automation}"          # path under the 210 login home
OFFBOX="${HA_DATASET_OFFBOX:-$HOME/Desktop/Backup/ha-dataset-backups}"   # //245/backup mount
DBS=(hot.db control.db mesh.db rungs.db weather.db)
# relocate-relevant + core dataset config — explicitly NON-secret (no *_secrets/vapid/auth/mqtt/*.env)
CFG=(areas.yaml devices.yaml device-placement.yaml house-geometry.json \
     area-migration.yaml device-rename.yaml tasmota-devices.yaml levoit-devices.yaml)

label="${1:-}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
name="dataset-${stamp}${label:+-${label}}"

echo ">> baseline snapshot: ${name}   (host=${HOST} repo=~/${RREPO})"

# ── 210 side: consistent snapshot + bundle (all in /tmp, read-only wrt production) ──
ssh "$HOST" "REPO=\"\$HOME/${RREPO}\"; W=\"/tmp/${name}\"; set -euo pipefail
  rm -rf \"\$W\"; mkdir -p \"\$W/db\" \"\$W/config\" \"\$W/parquet\"
  cd \"\$REPO\"
  for d in ${DBS[*]}; do
    [ -f \"instance/db/\$d\" ] && sqlite3 \"instance/db/\$d\" \".backup '\$W/db/\$d'\" && echo \"  snap \$d\" || echo \"  skip \$d (absent)\"
  done
  for c in ${CFG[*]}; do
    [ -f \"instance/\$c\" ] && cp -p \"instance/\$c\" \"\$W/config/\$c\" || true
  done
  if [ -d instance/db/parquet ]; then cp -a instance/db/parquet/. \"\$W/parquet/\"; fi
  {
    echo \"dataset baseline ${name}\"; echo \"utc \$(date -u +%FT%TZ)\"; echo \"host ${HOST}\"
    echo \"code_git_head \$(git rev-parse HEAD 2>/dev/null || echo unknown)\"
    echo; echo 'sha256:'; cd \"\$W\" && find . -type f ! -name MANIFEST.txt -exec sha256sum {} \; | sort -k2
  } > \"\$W/MANIFEST.txt\"
  tar -C /tmp -czf \"/tmp/${name}.tar.gz\" \"${name}\"
  mkdir -p \"\$REPO/instance/db/backups/baselines\"
  cp -p \"/tmp/${name}.tar.gz\" \"\$REPO/instance/db/backups/baselines/\"     # 210-local retained copy
  sha256sum \"/tmp/${name}.tar.gz\" | awk '{print \$1}' > \"/tmp/${name}.tar.gz.sha256\"
  du -h \"/tmp/${name}.tar.gz\" | cut -f1 | xargs echo '  bundle size:'
  rm -rf \"\$W\""

# ── pull off-box to the 245 backup share, verify integrity ──
mkdir -p "$OFFBOX"
scp -q "$HOST:/tmp/${name}.tar.gz"        "$OFFBOX/"
scp -q "$HOST:/tmp/${name}.tar.gz.sha256" "$OFFBOX/"
want="$(cat "$OFFBOX/${name}.tar.gz.sha256")"
got="$(sha256sum "$OFFBOX/${name}.tar.gz" | awk '{print $1}')"
ssh "$HOST" "rm -f /tmp/${name}.tar.gz /tmp/${name}.tar.gz.sha256"   # tidy 210 /tmp

if [ "$want" = "$got" ]; then
  echo ">> OK  off-box: ${OFFBOX}/${name}.tar.gz"
  echo ">>     sha256 verified end-to-end (${got})"
  echo ">>     210-local copy: ~/${RREPO}/instance/db/backups/baselines/${name}.tar.gz"
else
  echo "!! CHECKSUM MISMATCH — want=${want} got=${got}  (off-box copy is SUSPECT)"; exit 1
fi
