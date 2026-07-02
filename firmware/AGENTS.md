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
| `switchbot_decode` | **`firmware/components/`** ✓ | D1001 panel | pure, host-tested; verbatim lift from the byte-identical forks. Live edge nodes still link their fork copy until gated migration. |
| `ha_ble_scan` | **`firmware/components/`** ✓ | D1001 panel | shared NimBLE observer; controller-init + publish sink are caller hooks (native vs VHCI). Panel publishes canonical `home/edge/<node>/<mac>/adv`. Live edge nodes still on their `ble_scan` forks. |

The panel build (`~/reterminal-dev/d1001-beachhead`, off-repo dev tree; mirror at
[provisioning/reterminal/beachhead/](../provisioning/reterminal/AGENTS.md)) consumes these via
`components/<name>` **symlinks** into this tree — single source of truth stays here. Everything else remains
in the forks. Extraction order + rationale: [edge/MODULES.md](../edge/MODULES.md).

## Rules

- **Additive first.** Create the shared component and prove it in isolation *before* pointing any build at
  it. Live edge nodes migrate **gated, one at a time, re-validated** (Stage 2) — never big-bang.
- **Pure modules ship a host test** (`test/run.sh`, plain `cc`, no IDF) so correctness is provable off-target.
- A device is a **column in [MATRIX.md](../edge/MATRIX.md), not a fork.** When the generator lands, that
  table is produced from each build's `CMakeLists REQUIRES` and CI-checked against reality.
