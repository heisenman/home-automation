# Runbook — restore the dataset from an off-box baseline (`dataset_restore`)

Companion to `tools/backup-dataset.sh`. Rolls the operational dataset back to a baseline snapshot —
the safety net for the UI-driven data-movement (relocate/restamp) architecture. Relocate rewrites
`readings.area` across `hot.db` + the parquet archive; if a move goes wrong, restore from here.

> **GATED.** Restoring overwrites live DBs on the dictator (210). Stop the writers first, restore,
> restart. Hand Hugh the copy-paste; do not self-deploy a live-DB overwrite.

## What a baseline contains
`db/` WAL-safe `.backup` of hot/control/mesh/rungs/weather · `parquet/` the readings archive ·
`config/` relocate-relevant, **non-secret** registry + geometry (areas/devices/device-placement/
house-geometry/area-migration/device-rename/tasmota-devices/levoit-devices) · `MANIFEST.txt`
(UTC, code git HEAD, sha256 of every file). Secrets are deliberately NOT in the bundle.

## Locations
- Off-box (primary): `~/Desktop/Backup/ha-dataset-backups/<name>.tar.gz` (= `//245/backup`)
- 210-local (convenience): `~/home_automation/instance/db/backups/baselines/<name>.tar.gz`

## Verify before trusting a bundle
```
tar -xzf <name>.tar.gz -O <name>/MANIFEST.txt | head       # inspect
tar -xzf <name>.tar.gz && cd <name> && sha256sum -c <(grep -A999 '^sha256:' MANIFEST.txt | tail -n +2)
```

## Restore (on 210) — DBs
Consistent overwrite requires the writers stopped so nothing is mid-transaction.
```
# 1. quiesce writers
sudo systemctl stop ha-writer ha-api ha-api-tls        # + any ingest bridges touching the DBs
# 2. stage the bundle
cd ~/home_automation && mkdir -p /tmp/restore && tar -C /tmp/restore -xzf <path>/<name>.tar.gz
# 3. move current aside (never delete), drop the snapshot in
for d in hot.db control.db mesh.db rungs.db weather.db; do
  [ -f instance/db/$d ] && mv instance/db/$d instance/db/$d.replaced-$(date -u +%Y%m%dT%H%M%SZ)
  cp -p /tmp/restore/<name>/db/$d instance/db/$d
done
# 4. parquet archive (optional — only if the move corrupted it)
#    rsync --delete /tmp/restore/<name>/parquet/ instance/db/parquet/
# 5. config (optional — only if a registry edit is being rolled back)
#    cp -p /tmp/restore/<name>/config/<file> instance/<file>
# 6. restart
sudo systemctl start ha-writer ha-api ha-api-tls
```

## Peer (.245) note
`device_relocate` also rewrites `readings.area` on the .245 peer and backs the peer DB up first
(`instance/db/hot.db.bak-<stamp>` on 245). For a single bad relocate, the peer's own `.bak-<stamp>`
is the closest-in-time undo. A full baseline restore should be followed by a standby re-seed
(`failover/sync-standby.sh`) so 245 tracks the restored 210.

## After restore — sanity
```
sqlite3 instance/db/hot.db "SELECT area, COUNT(*) FROM readings GROUP BY area ORDER BY 2 DESC LIMIT 20;"
curl -sk https://192.168.0.210:8443/api/v1/rooms | python3 -c "import sys,json;d=json.load(sys.stdin);print('unlocated:',d['unlocated'])"
```
