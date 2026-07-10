# Runbook — rename / retire a device (`device_migrate`)

Renaming or removing a device from the system touches **many stores across both cluster boxes**. Doing it by
hand leaks — stale ntfy alerts, and (the sharp edge) the bidirectional `reconcile-history` merge **resurrects
the old id from the peer**. This is codified in `server/maintenance/device_migrate.py`.

> **Status: first-class feature.** The core is `run_migration(op, old, new, …) -> report dict` — pure
> orchestration, no CLI/HTTP coupling — so the eventual **admin "rename / remove device" control in the app**
> (`POST /api/v1/admin/devices/{id}:rename` / `:retire`) calls the exact same code the CLI does today.

## Use it

```bash
# always dry-run first — shows per-store counts + the peer count, writes nothing
venv/bin/python -m server.maintenance.device_migrate rename OLD_ID NEW_ID --dry-run
venv/bin/python -m server.maintenance.device_migrate rename OLD_ID NEW_ID          # do it (backs up first)

venv/bin/python -m server.maintenance.device_migrate retire  DEVICE_ID --dry-run
venv/bin/python -m server.maintenance.device_migrate retire  DEVICE_ID
```
Flags: `--no-peer` (skip .245), `--no-mqtt` (skip retained/alert cleanup), `--broker HOST`. Exit 0 = the old
id verified gone from local stores; exit 2 = residue remained (check the report). **Idempotent** — safe to
re-run. Every run backs up `hot.db` + `rungs.db` to `instance/db/backups/` and the peer's hot.db to
`hot.db.bak-<ts>` before mutating.

**rename vs retire:** use `rename` to move a device's history to a new id. Use `retire` to remove it — and
also when the new id already holds the data (a rename would collide on `UNIQUE(device_id, ts, metric)`).

## Cross-box migration: ACTIVATE on the destination BEFORE the source lets go

When a device moves to a **new home** (e.g. `.210 → ha-2`), the destination must actually **register** it —
not carry the source's "deregistered here" comment. If it doesn't, the device publishes live to the
destination broker but its ingest bridge sees UNKNOWN and drops/quarantines every reading — a **silent** loss
(`airgap_router_pm`+`failover_pm` bled ~23 h this way; [[migrated-device-silent-drop]], ADR-0032). `device_push`
gates on `confirm_on_ha2`, but a raw **config-sync + repoint** skips that path entirely — so assert it directly.

**Run ON the destination, after the move, BEFORE deregistering the source:**
```bash
# authoritative: checks the destination's real registries + hot.db + quarantine
venv/bin/python tools/migration_activate.py check <device_id>          # exit 0 = ACTIVATED
```
A device is **ACTIVATED** only when it is both **registered** (in a tasmota/levoit/ble/control registry,
uncommented) **and logging** (fresh `device_last_seen`) on its new home. If it's registered but not logging,
or not registered at all, the report says which — and lists any matching **quarantine** capture (where the
dropped readings actually went, mergeable losslessly via `tools/quarantine.py merge`). Only once `check`
passes is it safe to let the source deregister (drop the pending hold / remove the source registry entry).

**Standing safety net (any path):** `tools/migration_activate.py sweep` lists every live-but-unregistered
device on a box; `systemd/ha-migration-sweep.timer` runs it hourly and alerts `home/_alert/new`
(`live_but_unregistered_sweep`) so a missed activation can't bleed data unnoticed — even for migrations that
never touched `device_push`. Report-only; merge/purge stays the human-driven quarantine flow.

## What it touches (per host)

| Store | Rename | Retire | Why |
|---|---|---|---|
| `hot.db` — readings / device_last_seen / summaries | device_id → new | delete rows | live tier the UI + reconcile read |
| `rungs.db` — rung | device_id → new | delete rows | ADR-0022 ladder (also rebuilt from hot by `ha-rollup.timer`) |
| parquet `**` + `manifest.json` | rewrite col + rebuild manifest | drop rows + rebuild | forensic archive; manifest hashes feed the panel replica |
| retained MQTT | clear `home/<area>/<id>/state` + `home/_alerts` | same | else a phantom device lingers on displays / re-fires ntfy |
| **PEER `.245` hot.db** (over SSH) | same as local | same | **else `reconcile-history` re-inserts the old id every ~15 min** |

## The gotchas this encodes (learned the hard way, 2026-07-03)

- **The peer resurrects.** `failover/reconcile-history.sh` does a bidirectional `readings` merge every ~15 min
  (`INSERT OR IGNORE` on `UNIQUE(device_id, ts, metric)`). Migrate only .210 and the old rows keep coming back
  frozen at a pre-migration timestamp, while `device_last_seen` stays clean and ntfy keeps pinging "no data
  from `<old>`". Peer via `ssh -i ~/.ssh/id_cluster visko@$PEER_HOST` (from `instance/cluster.env`).
- **Alerts + retained topics** aren't in a DB — a stale `home/<area>/<id>/state` retained message drives the
  gap-watcher/ntfy alert and shows the device on `/api/v1/sensors`. Clear them or it looks alive.
- **rungs are derived** from hot.db by the 5-min rollup — so migrate hot.db, and rungs stay consistent (the
  tool migrates rungs too for immediacy).

## NOT handled by the tool (do these by hand — they're config / node decisions)

- **Registry** `instance/devices.yaml`: the reg-key→`device_id` map + `area`. For a device rename, edit the
  entry's `device_id`; the reg **key** is firmware-driven (`GAS_REG_KEY = HA_NODE_ID "-gas"`). Restart
  `ha-edge-mapper` (ingest) + **both** `ha-api` + `ha-api-tls` (catalog).
- **Node rename** (node_id, e.g. `c6-bench → coffice_c6`): firmware `HA_NODE_ID` + enrollment
  (`node_secrets.enc`) + `mesh.db` (`relay_state`/`mesh_links`). Reflash/OTA; see the c6/s3 rename history.

## Future: admin API

`run_migration` returns a JSON-able report `{op, old_id, new_id, hot, rungs, parquet, retained_cleared,
peer, verify_old_id_local, clean}`. The admin endpoint should: require admin auth, run `--dry-run` first and
surface the report for confirmation, then commit — reusing this module unchanged.
