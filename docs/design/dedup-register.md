# Firmware dedup + module-registration register (ADR-0020)

**Status:** open action items, seeded 2026-07-05 by `dev` after promoting `ha_gatt`/`ha_cmd`/`ha_ota` to
shared components. Coordinate dev/ops via the board (tasks `edge-core-dedup`, `edge-mqtt-wifi-dedup`,
`display-identity-gate`). This register is the "what's left" list; the pattern is proven (see
[../../edge/AGENTS.md](../../edge/AGENTS.md) → per-node identity + shared components).

## Why
Edge firmware is still "shared by copy" for a chunk of the tree: the same `.c` is forked into
`edge/esp32c6/main`, `edge/esp32s3-eth/main`, `edge/esp32c3/main` (and some into the panel). Forks drift
silently — exactly the class of bug that bit us on 2026-07-05 (a cross-provisioned image). ADR-0020 says
promote them to real shared IDF components in `firmware/components/` with platform seams (a `cfg` of
callbacks), consumed by every board. `ha_gatt`, `ha_cmd`, `ha_ota` are done; this is the remainder.

## Measured duplication (c6 baseline vs s3-eth; ×3 = also c3)
| Module | Lines | Drift (c6↔s3) | Copies | Verdict | Priority |
|--------|-------|---------------|--------|---------|----------|
| `gatt_exec.c` | 370 | **0 (identical)** | ×3 | promote to shared as-is | **P1** |
| `gatt_history.c` | 454 | **0 (identical)** | ×2 (s3,c3) | shared `ha_gatt` ALREADY exists — just wire s3/c3 to it (like ha_ota) | **P1** |
| `ha_relay.c` | 129 | **0 (identical)** | ×3 | promote to shared as-is | **P1** |
| `ha_sntp.c` | 56 | **0 (identical)** | ×3 | promote to shared as-is | **P1** |
| `ha_config.c` | 47 | **0 (identical)** | ×3 | promote to shared (keep per-node `secrets.h`/NVS seam) | **P2** |
| `ha_ota.c` (c3) | ~140 | n/a | ×1 left | wire c3 to the shared `ha_ota` (c6/s3 done) | **P2** |
| `ha_mqtt.c` | 367 | 50 | ×3 | shareable core + board seam (topics/log) — needs a seam pass | **P3** |
| `ha_gas.c` | 117 | 104 | ×2 | converge sensor-select (#if) so one file serves both | **P3** |
| `ha_wifi.c`/`ha_eth.c` | 57 | 42 | ×3 | genuinely per-transport (WiFi / W5500 / esp-hosted) — keep split OR thin `ha_net` seam | **P4** |
| `app_main.c` | 93 | 71 | ×3 | board bring-up glue — least shareable; leave forked, document the shape | **P4** |

**P1 alone removes ~950 lines of identical triplicated source** and closes the highest-drift-risk copies.

## STATUS — 2026-07-06: edge-core dedup COMPLETE (all cleanly-shareable modules shared)
Every module whose drift was *cosmetic* (byte-identical or board-hook-only) is now a shared component in
`firmware/components/`; the forks are deleted and all 3 edge builds + the panel are build-validated:

| Module | Result |
|--------|--------|
| `gatt_exec` → `ha_gatt_exec` | **shared ×3** (cfg seam reply/log; Kconfig write-gate). DEPLOYED (cbed/coffice OTA, hbed_s3 bench). |
| `gatt_history` → `ha_gatt` | **shared** c3/c6/s3/panel |
| `ha_relay`, `ha_sntp` | **shared ×3** (plain promote, 0-drift) |
| `ha_ota` | **shared** c3/c6/s3/panel |
| `ha_config` | **shared ×3** (secrets seam: app_main passes compile-time defaults; component secrets-free) |
| `ha_mqtt` → `ha_mqtt` | **shared ×3** (biggest: ~700 lines removed; `ha_mqtt_init` seam = per-node secrets + LED hooks + reach flag; gatt/ota/exec seams internal) |

**Kept forked — genuine board/platform glue (drift encodes real differences, NOT triplication):**
- `ha_gas` (c6/s3): compile-time **per-node sensor select** (SGP30 vs SGP40 via `secrets.h`), board I2C pins,
  per-sensor metrics (eCO₂/TVOC vs VOC-index), per-node reg-key. Sharing cleanly would need linking both
  drivers (runtime select, abandoning the elegant per-node compile-time select) or a thin shell + still-forked
  sensor read — worse than a clean fork. **Verdict: node-glue, leave forked** (P3 drift-analysis conclusion).
- `ha_wifi` (c3/c6 identical, s3 diverged): s3 adds a down-watchdog (reboot-on-outage) + reconnect-forever +
  LED hooks — real robustness divergence. **Platform, leave forked** (or a future `ha_net` seam if desired).
- `ha_eth` (s3 W5500), `ha_led` (s3 WS2812), `app_main` (board bring-up glue, P4): board-specific by nature.

## Registration gaps (doc ↔ reality drift)
- `edge/MODULES.md` labels `gatt_exec`, `ha_relay`, `ha_sntp` as **shared**, but they are still **fork** in
  `edge/MATRIX.md` (the generated truth). Fix as each is promoted — MODULES.md was aspirational.
- Every new shared component needs a parseable `BREADCRUMB:` header (pinned by `tests/test_module_matrix`
  + `gen_reuse.py --check`) and a row in `MODULES.md`/`MATRIX.md` (regenerate, don't hand-edit).
- New shared components must be added to each consuming board's `EXTRA_COMPONENT_DIRS` + `main` `REQUIRES`.

## Suggested ownership
- **dev:** P1 + P2 (edge core: gatt_exec, ha_relay, ha_sntp, ha_config, wire s3/c3 to shared ha_gatt +
  c3→shared ha_ota). Board task `edge-core-dedup`.
- **dev:** P3 (ha_mqtt/ha_gas seam passes) — board task `edge-mqtt-wifi-dedup`.
- **ops:** panel (beachhead) adoption of the shared components it can use (`ha_ota`, `ha_gatt`, `ha_cmd`
  already; `ha_relay`/`ha_sntp` next) — folds into `display-identity-gate`.
- **P4** (`ha_wifi`/`app_main`): document as intentionally per-board unless a clean seam emerges.

## Method (per module, proven on ha_ota)
1. `diff` the forks to find the real board seam (usually a log/LED/transport call).
2. Copy one fork → `firmware/components/<m>/`; replace platform deps with `cfg` callbacks (`<m>_init`).
3. Add `BREADCRUMB:` header; delete the forks; wire each board's CMake + an `<m>_init` cfg.
4. Build every consuming board; regenerate `REUSE.md`/`MATRIX.md`; run the suite.
