# Runbook — relocate a device / an area (`device_relocate`)

Moving a device to a new **`area`** (its *location*) is a different concern from renaming its **`device_id`**
(its *identity* — that's [`device_migrate`](runbook-device-migrate.md)). They are kept as **separate, atomic
tools + runbooks** on purpose: identity and location fail differently, and you rarely want both at once.

`area` is *denormalised* — stamped onto every reading at ingest — so a history-safe relocate rewrites it in
every store it was written to, **and** the registry (the authoritative source the edge-mapper reads; if that
isn't updated the mapper re-stamps the old area on the next reading and the drift comes straight back). This
is codified in `server/maintenance/device_relocate.py` (ADR-0026 Phase 2).

> **Status: first-class primitive.** The core is `relocate(kind, key, new_area, …) -> report dict` — pure
> orchestration, no CLI/HTTP coupling — so a future admin "move device" control can call the same code.

## Use it

```bash
# ALWAYS dry-run first — shows per-store counts + the peer count, writes nothing
venv/bin/python -m server.maintenance.device_relocate area   OLD_AREA NEW_AREA --dry-run
venv/bin/python -m server.maintenance.device_relocate area   OLD_AREA NEW_AREA            # do it (backs up first)

venv/bin/python -m server.maintenance.device_relocate device DEVICE_ID NEW_AREA --dry-run # override one device
venv/bin/python -m server.maintenance.device_relocate device DEVICE_ID NEW_AREA

# apply a whole ADR-0026 crosswalk (renames + merges as area moves, then device_overrides last so they win)
venv/bin/python -m server.maintenance.device_relocate crosswalk instance/area-migration.yaml --dry-run
venv/bin/python -m server.maintenance.device_relocate crosswalk instance/area-migration.yaml
```

Flags: `--no-peer` (skip .245), `--no-mqtt` (skip retained cleanup), `--no-registry` (data stores only),
`--broker HOST`. Exit 0 = no stale-area rows remain locally; exit 2 = residue remained (check the report).
**Idempotent** — safe to re-run. Every run backs up `hot.db` **and every registry file** to
`instance/db/backups/` (and the peer's hot.db to `hot.db.bak-<ts>`) before mutating.

**area vs device:** use `area` to move *everything* in a location (a rename like `h_bedroom→h_bed`, or a merge
like `office→h_office`). Use `device` to override *one* device to an area different from its area's mapping
(e.g. `host_210` should follow the rack, not the office it currently sits in). In a crosswalk, overrides run
**after** the area moves so they take precedence.

## What it touches (per host)

| Store | Effect | Why |
|---|---|---|
| `hot.db` — readings / device_last_seen | `area` → new (by area or by device_id) | live tier the UI reads |
| parquet `**` + `manifest.json` | rewrite `area` col + rebuild manifest | forensic archive; manifest hashes feed the panel replica |
| registry — `instance/{devices,control,*-devices}.yaml` | rewrite the `area:` line (quote/indent/comment preserved) | **authoritative** source the edge-mapper re-stamps from |
| retained MQTT | clear old `home/<old_area>/<id>/state` | else a phantom device lingers under the old room on displays |
| **PEER `.245` hot.db** (over SSH) | same area update | consistency (lower-stakes than a migrate — see below) |

**NOT touched:** `rungs.db` / `summaries` — neither has an `area` column (they key off `device_id`, which a
relocate does not change), so they stay correct automatically.

## After the run — reload the mapper (required)

The registry is the mapper's source of truth, so it must reload for new readings to carry the new area, and
both APIs must reload the catalog:

```bash
sudo systemctl restart ha-edge-mapper ha-api ha-api-tls
```

Then confirm the guard is green: `venv/bin/python -m tests.test_areas` (goes GREEN once every config `area:`
resolves to an `instance/areas.yaml` id — the ADR-0026 acceptance).

## Gotchas this encodes

- **Update the registry or it drifts right back.** Unlike a `device_migrate` (where the registry edit is an
  optional config decision), for a relocate the registry **is** the point — the mapper re-stamps `area` from
  it on the very next reading. `--no-registry` is for data-only backfills, not normal use.
- **The peer is lower-stakes here than in a migrate.** `area` is **not** part of `reconcile-history`'s
  `UNIQUE(device_id, ts, metric)` key, so a peer left un-updated will **not** resurrect the old area into
  .210 (the rows already exist; `INSERT OR IGNORE` skips them). We still update the peer for consistency; skip
  with `--no-peer` if .245 is down. Contrast `device_migrate`, where the peer genuinely resurrects.
- **Retained topic path is keyed by area** (`home/<area>/<id>/state`). The old-area retained message is cleared
  so the device stops appearing under its old room; the mapper republishes under the new area on the next read.
  The `device_id` is unchanged, so the gap-watcher does **not** fire a "no data" alert (unlike a rename).

## Future: admin API

`relocate` returns a JSON-able report `{kind, key, new_area, affected_devices, hot, parquet, registry,
retained_cleared, peer, verify_stale_local, clean}`. An admin endpoint should require admin auth, run
`--dry-run` first and surface the report for confirmation, then commit — reusing this module unchanged.
