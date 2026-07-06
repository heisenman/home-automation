# Ingest & registry-flow map

**What this is:** the map of how a device's canonical identity/area gets stamped onto readings, which
long-running services cache the registry, and — the practical payoff — **what "everything" is when you
propagate a change that touches device identity/area or the ingest path.** Written out of the
`relocate-ingest-reload` fix (2026-07-06), where a UI relocate "popped back" because the ingest bridges
cached the registry at startup and never reloaded.

Related: [relocate-ingest-reload.md](relocate-ingest-reload.md) (the root-cause write-up + fix plan this
implements), [area-migration-runbook.md](area-migration-runbook.md) (the relocate procedure itself),
[../runbook-dataset-restore.md](../runbook-dataset-restore.md) (backup/restore safety gate),
`server/maintenance/device_relocate.py` (the relocate primitive), the failover propagation surface in
[../../failover/dictator-files.manifest](../../failover/dictator-files.manifest).

## The area-stamping ingest components

Every one of these is a **long-running service** that loads a device registry **once at startup** and
stamps each reading's `device_id` / `area` from it. They are the set that must honour a registry edit
(a relocate/rename) — historically the blind spot, now covered by the mtime-reloader below.

| Service (systemd)      | Entrypoint                          | Registry file (`--registry`)      | Keyed by            | Class          |
|------------------------|-------------------------------------|-----------------------------------|---------------------|----------------|
| `ha-scanner`           | `server/ingest/scanner.py` (script) | `instance/devices.yaml`           | MAC (upper)         | `Scanner`      |
| `ha-edge-mapper`       | `-m server.ingest.edge_mapper`      | `instance/devices.yaml`           | MAC                 | `EdgeMapper`   |
| `ha-edge-history`      | `-m server.ingest.edge_history`     | `instance/devices.yaml`           | MAC                 | `HistoryIngest`|
| `ha-tasmota-bridge`    | `-m server.ingest.tasmota_bridge`   | `instance/tasmota-devices.yaml`   | Tasmota topic name  | `TasmotaBridge`|
| `ha-levoit-bridge`     | `-m server.ingest.levoit_bridge`    | `instance/levoit-devices.yaml`    | ESPHome node name   | `LevoitBridge` |

Notes:
- `scanner.py` runs as a **script** (its own dir on `sys.path`) so it can `from decoders import …`
  (`server/ingest/decoders/`); the other four run as `-m` modules. Keep that in mind when importing them.
- Each defines its own `load_registry(path)` (different key normalisation per source), so the reloader
  takes the bridge's own loader — it is not a single shared parser.
- `edge_history` was **not** in Ops's original 4-bridge list but stamps area the same way (GATT-history
  reassembly → `hot.db`); it is included for the same reason. If you add a new ingest path that stamps
  area, it belongs in this table **and** needs the reloader (below).

## The data flow — why a relocate "popped back"

```
devices.yaml (canonical area)                  device_relocate.py edits this  (ops-owned primitive)
      │  load_registry() at startup (cached)
      ▼
ingest bridge  ──stamps area──▶  home/<area>/<device>/state (MQTT)
      │
      ▼
server/storage/writer.py:176   INSERT INTO device_last_seen(...area...) ON CONFLICT DO UPDATE SET area=excluded.area
      │                        ← the NEXT reading overwrites area back to the bridge's cached value
      ▼
server/api/viewmodel.py:146    build_sensor_list LEFT JOIN device_last_seen d  → room = meta.room OR d.area
      ▼
GET /api/v1/rooms  → PWA / D1001 panel                (never reads devices.yaml directly)
```

The display room comes from `device_last_seen.area`, which the writer refreshes from **whatever area
the bridge stamps**. So relocating a device by editing `devices.yaml` had no effect on live ingest — the
bridge kept its stale cache, the next reading re-stamped the old area, and the UI reverted within one
scan cycle. Devices only "stuck" if their bridge happened to be restarted.

## The fix — live registry reload (`server/util/registry_reload.py`)

`RegistryReloader(path, loader)` watches the registry file's **mtime** and reloads via the bridge's own
`load_registry` when it changes; `.current()` on the hot path returns the freshest registry. So a
relocate's `devices.yaml` edit takes effect on live ingest within one poll — **no restart, no
cross-process signal** (`device_relocate.py` needs no change). Properties:

- **Throttled `stat()`** (`throttle_s`, default 2 s) so a high-frequency hot path (BLE adverts) doesn't
  hammer the filesystem.
- **Torn-read safe:** `device_relocate` rewrites the file non-atomically (`path.write_text`), so a poll
  can catch a half-written file. A failed reload keeps **both** the previous value **and** the previous
  mtime, so the next poll retries — the update is never silently lost.
- **Missing-file safe:** `stat()` → `None` keeps the last-known-good registry (never clobbers to empty).

Each bridge is wired the same, backward-compatibly:
- `__init__` stores `self._registry_get = lambda: registry` (a plain dict still works — the unit tests
  construct bridges with a dict).
- `attach_reloader(reloader)` swaps in `reloader.current` for the live path (called in `main()`).
- `self._registry` is a **property** → `self._registry_get()`. Derived caches follow it:
  `LevoitBridge._by_devid` is now a property recomputed from `_registry`.

### control.yaml is a SECOND reloaded registry (the 6th + 7th area-stampers)

`devices.yaml` isn't the only cached registry. **Actuator** areas live in `instance/control.yaml`, which
is cached by two more long-running services — so an *actuator* relocate popped back even after the 5
ingest bridges were fixed:

- **6th — `ha-controller`** (`server/control/controller.py`): the Midea dehumidifier is a controller-side
  local driver; `_publish_state` stamps `area` from `self.registry` (loaded from control.yaml at
  startup). `attach_registry_reloader(RegistryReloader(control.yaml, load_control_registry))` +
  `_refresh_registry()` at the top of each `tick()` swaps `self.registry` live. Only the area/indicator
  path reloads; the issuer/drivers/transports keep their mount-time state (adding a device still needs a
  restart, but a relocate flows through).
- **7th — `ha-api`/`ha-api-tls`** (`server/api/main.py`): `app.state.control_registry` feeds the READ
  side (`build_actuator_list` → `/rooms`, `build_display` → `/displays`). `_control_registry()` consults
  an `app.state.control_registry_reloader` so a relocate shows up without an ha-api restart.

The shared `RegistryReloader` now lives at `server/util/registry_reload.py` (used by ingest, control, and
api). Structural follow-up under review: `docs/design/actuator-telemetry-contract.md` proposes stamping
actuator `area` in ONE place so Midea (controller) and Levoit (bridge) can't drift again.

Tests: `tests/test_registry_reload.py` (reload-on-change, throttle, torn-read recovery, missing-file);
existing `tests/test_edge_mapper_dedup.py` proves the dict constructor still works.

## Propagation checklist — "update everything"

When a change touches device **identity/area** or the **ingest path**, the full surface is:

1. **Registry files** (`instance/devices.yaml`, `tasmota-devices.yaml`, `levoit-devices.yaml`) — the
   canonical inputs. In the failover allowlist (`failover/dictator-files.manifest`) so a promoted
   standby / fresh server inherits them; a relocate edit is now picked up live by the reloader.
2. **The 5 ingest services** in the table above — restart on 210 to pick up **code** changes (a registry
   *data* edit no longer needs a restart). This is the "gated on 210" step (touches live ingest on the
   dictator; coordinate via the board).
3. **Writer / consumers** — `server/storage/writer.py` (`device_last_seen`), `server/api/viewmodel.py`
   (`/api/v1/rooms`). The API fronts are **VIP-gated**: control-plane routes only mount on the VIP
   holder — see `server/api/main.py:_mount_control`. Both fronts run `server.api.main:app`:
   `ha-api-tls` (:8443, HTTPS) and `ha-api` (:8123). Restart both on a route/catalog change.
4. **Failover peer (.245)** — `git` fast-forward + restart its active services. Control-plane 404 on the
   warm standby is **by design** (it isn't the VIP holder); the code still propagates for post-promotion.
5. **Edge nodes** — dumb by ADR-0001 (the dictator owns the registry); no per-node registry to push.
