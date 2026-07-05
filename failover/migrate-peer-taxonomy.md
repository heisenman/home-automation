# Runbook — migrate the peer (.245) to the ADR-0026 taxonomy

**Why:** the dictator (.210) was renamed/relocated to the canonical taxonomy (ADR-0026, in-session). The peer
(.245) still runs its own ingest under the OLD registry, so `ha-reconcile-history` keeps merging old
ids/areas back into .210. Bringing .245 in line stops that.

**Who:** run on **.245** (ops has direct access; or Hugh after `git pull`). Script: `failover/migrate-peer-taxonomy.sh`.

## Steps

1. On .245: `cd ~/home_automation && git pull` (gets the tools + config-driven controller).
2. Copy the 3 gitignored inputs from .210 (they never enter git):
   ```
   # from .210:
   scp instance/{areas,area-migration,device-rename}.yaml visko@192.168.0.245:~/home_automation/instance/
   ```
3. On .245, dry-run then apply:
   ```
   ./failover/migrate-peer-taxonomy.sh --dry-run
   ./failover/migrate-peer-taxonomy.sh
   ```
   It runs the area crosswalk then the rename/relocate worksheet with `--no-peer` (the peer is .210, already
   done), restarts .245's ingest fleet, sweeps stragglers, and verifies `test_areas`. Idempotent.

## Coordinating with reconcile

While .245 migrates, `.210`'s reconcile could still pull a few of .245's not-yet-migrated old-id rows. Two
clean options:
- **Simplest:** run the script on .245; when it finishes, on **.210** clear any residual old-id rows once
  (re-run `venv/bin/python -m server.maintenance.apply_rename_worksheet` on .210 — idempotent — it sweeps
  what reconcile pulled in). After .245 is fully migrated, nothing regenerates them.
- **Tightest:** briefly stop `ha-reconcile-history` on **both** boxes for the ~2 min the .245 run takes,
  then start both.

## Verify (on both boxes afterward)

```
venv/bin/python -m tests.test_areas                      # GREEN
sqlite3 instance/db/hot.db "SELECT DISTINCT device_id FROM device_last_seen;"   # only new ids
```
