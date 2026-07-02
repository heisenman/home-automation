# firmware/ — shared edge/panel firmware core (ADR-0020)

**Target tree** for the module merge that retires the `edge/*/main/` `cp -r` fork tax. Populated
**incrementally**, panel-first, lowest-risk module first. Until a module lives here, its canonical copy is
still the fork under [edge/](../edge/AGENTS.md); [MATRIX.md](../edge/MATRIX.md) is the source of truth for
which build links which.

```
firmware/
  components/<module>/     real shared IDF components (header states contract + platform support)
    <module>.c
    include/<module>.h      public header
    CMakeLists.txt          idf_component_register(...)
    README.md               contract, platform support, ADR ref
    test/                   host unit test + run.sh (no IDF needed) where the module is pure
  devices/<device>/        (future) thin per-device builds: platform shim + REQUIRES-picked modules
```

## Migration status (ADR-0020 Stage 1)

| Module | Home | Consumed by | Notes |
|--------|------|-------------|-------|
| `switchbot_decode` | **`firmware/components/`** ✓ | **all builds** (panel + c3/c6/s3) | pure, host-tested; verbatim lift. Fork copies retired fleet-wide. |
| `ha_ble_scan` | **`firmware/components/`** ✓ | **all builds** (panel + c3/c6/s3) | shared NimBLE observer; controller-init + publish sink + `shared_radio` duty-cycle are caller hooks/flags (native vs VHCI; WiFi coexistence). The s3 duty-cycle drift is reconciled here. Fork copies retired. |

**Fully migrated (ADR-0020 Stage 2 complete for the BLE core).** Consumers link the shared components via
`REQUIRES` + `EXTRA_COMPONENT_DIRS ../../firmware/components` (edge nodes) or `components/<name>` **symlinks**
(the panel, off-repo dev tree at `~/reterminal-dev/d1001-beachhead`). Each node keeps a 2-line `ble_scan.h`
shim so `gatt_*`/`ha_ota` includes are untouched. Still forked (future stages): `ha_mqtt` (3×-drifted),
`app_main`, `gatt_*` (Stage 2 → shared `ha_gatt`), and the platform modules. Extraction order + rationale: [edge/MODULES.md](../edge/MODULES.md).

## Rules

- **Additive first.** Create the shared component and prove it in isolation *before* pointing any build at
  it. Live edge nodes migrate **gated, one at a time, re-validated** (Stage 2) — never big-bang.
- **Pure modules ship a host test** (`test/run.sh`, plain `cc`, no IDF) so correctness is provable off-target.
- A device is a **column in [MATRIX.md](../edge/MATRIX.md), not a fork.** When the generator lands, that
  table is produced from each build's `CMakeLists REQUIRES` and CI-checked against reality.
