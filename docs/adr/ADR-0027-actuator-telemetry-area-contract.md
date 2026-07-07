# ADR-0027 — Actuator telemetry / area-stamping contract: one stamp, one source

**Date:** 2026-07-06
**Status:** **Accepted** (Hugh, 2026-07-07). Dev-authored from `docs/design/actuator-telemetry-contract.md`
(ops) + dev review; ops ACKed Option 3 + both additions (board `actuator-telemetry-contract`, 2026-07-06).
Implemented + deployed (shared stamp helper `server/control/actuator_state.py`, Levoit single-source; 210 +
.245). Remaining rollout step 4 (remove `levoit-devices.yaml:area` + drift-guard test) is a follow-up.
**Builds on:** ADR-0026 (canonical area taxonomy), the registry live-reload pattern
(`server/util/registry_reload.py`), ADR-0001 (dictator owns the registry).
**Related:** ADR-0011 (control loop), ADR-0013 (BFF = UI truth), `docs/design/ingest-registry-map.md`
(the 7 area-stampers), the `controller-area-reload` point-fix (a748113).

## Context — the command path is unified; the telemetry/area path is not

Outbound commands conform to one contract (`CommandIssuer` + `RoutingTransport`). Inbound **state +
`area` stamping does not**. Each actuator family gets its state in — and stamps `area` — a different way,
in a different place:

| | **Midea (dehumidifier)** | **Levoit (purifier)** |
|---|---|---|
| State source | controller-side local driver `MideaDriver.status()`, **polled** each `controller.tick` | **pushed** by the standalone `levoit_bridge.py` ingest process |
| Who stamps `area` | `ha-controller._publish_state` | `levoit_bridge._emit` |
| Area source | `control.yaml` (via `self.registry`) | **`levoit-devices.yaml`** `area:` field |

Two consequences, both already bitten:
1. **Two stamping sites to keep in sync by hand** → a fix to one misses the other. The relocate "pop-back"
   was fixed in the 5 ingest bridges but not the controller (`controller-area-reload`, the 6th stamper);
   the API read-path was a 7th.
2. **Two area SOURCES for actuators** (`control.yaml` vs `levoit-devices.yaml`) that can silently diverge.

## Decision

**One shared stamping helper, one area source.**

1. **Shared stamp helper (Option 3).** A single function builds every actuator's canonical `/state`
   payload — `{schema, device_id, device_type, area, ts, transport, running, target_pct, metrics, meta}` —
   and both `ha-controller._publish_state` and every actuator-publishing bridge (`levoit_bridge`) call it.
   Neither family computes `area` itself, so neither can drift. Home: `server/control/actuator_state.py`
   (importable by both the control loop and the ingest bridges).
2. **`area` is authoritative from the reload-aware CONTROL registry (`control.yaml`), for BOTH paths.**
   - **Write/stamp path:** the helper resolves `area` from the control registry, keyed by `device_id`.
   - **Direct-read path:** `build_actuator_list` (`/rooms`) and `build_display` (`/displays`) read the
     control registry directly, bypassing telemetry — they MUST be equally reload-aware. (Already true as
     of `controller-area-reload`: `_control_registry()` + `app.state.control_registry_reloader`.) This
     addition is not optional — the 7th-stamper find proves a stale *read* cache re-introduces the bug.
3. **Single source (the Levoit migration).** `levoit-devices.yaml` keeps only what the bridge alone knows:
   the **ESPHome-node-name → `device_id`** mapping. Its `area:` field is **deprecated**. The bridge
   resolves `area` from `control.yaml` (control registry, reload-aware) by `device_id`. Rejected: Option 2
   (route Levoit's push through the controller — a double hop) and Option 1 (a `StatusSource` protocol —
   over-build for two families; revisit on a third).

**Kept as-is:** the command path (`RoutingTransport` + `CommandIssuer`); heterogeneous backends (poll a
local driver vs consume a bridge) stay allowed — only the *state output + stamping* conforms.

## Levoit area migration (ops's required ADR item)

So nothing reads the old source post-cutover:
1. **Add** a `control.yaml` reloader to `levoit_bridge`; resolve `area` by `device_id` from the control
   registry.
2. **Deprecate** `levoit-devices.yaml:area`. During transition the bridge logs a WARNING if the file still
   carries an `area` that disagrees with `control.yaml` (surfaces drift instead of hiding it).
3. **Fallback** only if the `device_id` is absent from `control.yaml` (a Levoit device not yet declared as
   a control actuator): keep the old `area` field, log that it's the deprecated path.
4. **Remove** the `area:` field from `levoit-devices.yaml` once (2) is quiet on the live box; the file is
   then purely the name→id map. A drift-guard test asserts no `*-devices.yaml` actuator carries an `area`
   that disagrees with `control.yaml`.

## Consequences

- **+** Location stamping stops being whack-a-mole: one helper, one source. A relocate edits `control.yaml`
  and every path (both families' telemetry + both read endpoints) follows via the live reload.
- **+** New actuator family = implement its state-getter, call the shared helper — area/identity are free.
- **−** `levoit_bridge` gains a `control.yaml` dependency (it was `levoit-devices.yaml`-only). Acceptable:
  the dictator owns the control registry (ADR-0001) and the bridge runs on-box.
- **−** A brief transition where `levoit-devices.yaml:area` still exists but is ignored (warned on drift).

## Rollout

1. Land the shared helper + wire `ha-controller` (behavior-neutral: same payload it emits today).
2. Wire `levoit_bridge` onto the helper + `control.yaml` area; keep the fallback + drift-warning.
3. Verify a **Midea** and a **Levoit** relocate BOTH stick live (no restart, no pop-back).
4. Remove `levoit-devices.yaml:area` + add the drift-guard test. The `controller-area-reload` point-fix
   already shipped independently and is subsumed here.
