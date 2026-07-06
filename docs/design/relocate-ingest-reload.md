# UI relocate "pops back" — ingest bridges cache the registry (dev+ops)

**Status:** root cause found + evidenced; fix owned by dev (ingest, gated on the dictator). Board task
`relocate-ingest-reload`.

The UI-driven device relocate (panel devices-screen → `POST /api/v1/devices/{id}/relocate`) *runs and
persists*, but a relocated device reverts to its original room within seconds. This is not a lost write —
it's that nothing which drives the *displayed* room actually reads the write, and the live ingest
overwrites the DB back.

## Root cause (all confirmed on the running dictator, not hypothesized)

1. **The endpoint is deployed and works.** `POST /api/v1/devices/{id}/relocate` returns 401 (auth-gated)
   on both `:8123` (ha-api, the panel's target) and `:8443` (ha-api-tls); `ha-api` was restarted after the
   deploy. `device_relocate.relocate()` genuinely ran — proven by fresh `instance/db/backups/hot.db.*.bak`
   snapshots it writes before mutating.

2. **The displayed room is derived from `device_last_seen.area`, not the registry.**
   `build_sensor_list` ([server/api/viewmodel.py](../../server/api/viewmodel.py) ~L141-146) `LEFT JOIN
   device_last_seen d` and takes `d.area`; L174 sets `room = meta.room or area`. It **never reads
   `devices.yaml`.** So editing the registry alone changes nothing on screen.

3. **The live ingest bridges load the registry once at startup and never reload.** The BLE scanner
   ([server/ingest/scanner.py](../../server/ingest/scanner.py)) calls `load_registry()` in `__init__` and
   `run()` uses the cached `self._registry`; there is no reload/watch path. It keeps publishing readings
   stamped with the **old** area. Same shape in `edge_mapper.py`, `tasmota_bridge.py`, `levoit_bridge.py`
   (each stamps `area` from a registry read at start).

4. **Every new reading overwrites the pointer back.** `writer.py` (~L176-183) upserts
   `device_last_seen ... ON CONFLICT DO UPDATE SET area=excluded.area`. So the next reading (seconds later)
   resets `device_last_seen.area` to the old value → the room view flips back → **"pops back."**

Net: `relocate()` correctly rewrites `hot.db` (readings + pointer) and `devices.yaml`, but the read path
ignores `devices.yaml` and the ingest re-stamps the pointer old. Devices whose bridge *happened to be
restarted* right after a relocate stuck; the BLE meter devices (bridge long-lived) revert.

## The fix (dev — ingest domain, gated restart on the dictator)

Make the ingest bridges **reload the registry when it changes**, so a relocate's `devices.yaml` edit takes
effect on live ingest. Then new readings stamp the new area → `device_last_seen` becomes new → the view is
correct **and** forward-history is correctly attributed.

- **Bridges to fix** (each stamps `area` from a cached registry): `scanner.py` (BLE — the primary case),
  `edge_mapper.py`, `tasmota_bridge.py`, `levoit_bridge.py`.
- **Recommended mechanism: watch the registry file mtime and reload on change** (self-contained per bridge,
  no cross-process signalling, robust across restarts). With this, `device_relocate.py` needs **no change** —
  it already edits `devices.yaml`; the bridges pick it up on their next loop.
- **Alternative:** a `SIGHUP` handler in each bridge that re-runs `load_registry()`, plus `device_relocate`
  sending `SIGHUP` to the ingest units after it edits the registry. More plumbing; only if mtime-watch is
  awkward for a given bridge.

### Verification
Relocate a long-lived BLE device, wait one scan cycle (~a minute), then confirm:
- new `readings` rows for it carry the **new** area (`SELECT ts,area FROM readings WHERE device_id=? ORDER BY
  ts DESC LIMIT 5`),
- `device_last_seen.area` == new area,
- `GET /api/v1/rooms` shows it under the new room **and it stays there** on the next refresh.

## Open question for dev
Should `build_rooms`/`build_sensor_list` read the **canonical registry** (`devices.yaml` area) for the
*current* room instead of `device_last_seen.area`? That would make the displayed room authoritative and
independent of ingest timing (consistent with the locked data model: room = canonical assignment;
`readings.area` is the frozen per-reading history). The mtime-reload fixes the symptom either way, but this
decides whether "current room" is sourced from the assignment or from the last reading's stamp. Dev's call.

## Ownership boundary
- **ops:** panel devices-screen (`main/ui/ui_devices.c`, v84 shipped), the endpoint (`server/api/control.py`),
  the `device_relocate` primitive (`server/maintenance/device_relocate.py` — restamp + forward modes).
- **dev:** the ingest-bridge reload + the gated restart of the ingest units on the dictator.
- **Blocked on this (ops):** reformatting the devices screen to editable Name / Location / Status columns —
  held until relocate sticks (the Location column would pop back the same way).
