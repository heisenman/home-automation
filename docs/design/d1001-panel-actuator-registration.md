# D1001 panel-as-actuator — registration + validation (roadmap #2, interim/unsigned)

**Date:** 2026-07-04  **Status:** code shipped; registration + live-validate pending (gated on the dictator).
**Roadmap:** [d1001-capability-roadmap.md](d1001-capability-roadmap.md) #2 · ability **B** (ADR-0002/0014).

## What shipped (in-repo, ungated)
The D1001 wall panel becomes a **first-class controllable device**: the house scene drives its backlight
through the normal control plane (issuer → PEP), instead of the bespoke `cmd/screen` side channel.

- **Firmware** (`provisioning/reterminal/beachhead`, tag `v60-panel-actuator`):
  - `switchable` — already lived on `cmd/screen` (`on`/`off`/`toggle`).
  - `setpoint` — **new** `cmd/brightness N` (0–100) → `bsp_display_brightness()`; `0` = screen off.
  - **retained** `state/screen` = `{"on":bool,"level":pct}` (level = *effective* backlight, 0 when off) so the
    server driver reconciles intended-vs-reported (**ADR-0014 R3**). Re-asserted on (re)connect + every change.
- **Server**:
  - `server/control/panel_driver.py` — `PanelMqttTransport`, a plain-MQTT local driver mirroring
    `levoit_driver.py`. `switchable→cmd/screen`, `setpoint→cmd/brightness`; reads `state/screen` back.
  - `bootstrap.py` — routes panel `device_id`s to `PanelMqttTransport` via `RoutingTransport` (auto-loads
    `instance/panel-devices.yaml`, mirroring the Levoit side-registry).
  - `controller.py` — `_apply_panel_scene()`: **edge-triggered scene-follower**. On a house-scene change it
    issues `setpoint = scene_brightness(policy, scene)` to each `device_type: panel`. Panels are **not**
    sensor-reconciled (skipped in the closed loop) — this is their whole control rule. A manual PWA brightness
    set is left alone between scene switches.
  - `automation.scene_brightness()` — per-scene backlight: scene patch `brightness` → base `brightness` → None.
  - `api/control.py` — policy `scenes.<name>.brightness` (0–100) is now a valid, validated key.
  - `registry.py` / `issuer.DeviceCtl` — `device_type` (ADR-0014 R7 registry field) is now parsed/carried.
- **Tests:** `tests/test_panel_driver.py` (7) + a `brightness` case in `test_control_api.py`. Green.

## Posture — interim, unsigned (deliberate)
A controllable actuator *receives* commands, which per **ADR-0010** should be **signed** (ability C). The panel
is **not an enrolled node**, so #2 rides the existing **unsigned-LAN `cmd/*`** path (a local driver, LAN-topological
trust — exactly like Levoit/Midea). Promote to the signed `MqttTransport` when **roadmap #4 (cmd enrollment)**
lands. Consequence: the panel will appear in the boot "no command secret (unknown-device)" warning — **expected**
for a local-driver device (it is commandable via its override transport); harmless until #4.

## Registration — GATED (dictator `instance/`, hand-applied by Hugh)

**1. `instance/control.yaml`** — add the panel device (set `area` to where it actually lives):
```yaml
devices:
  d1001_panel:
    node: d1001-beachhead        # MQTT topic root / node identity
    area: c_office               # <-- set to the panel's real room
    device_type: panel
    traits:
      switchable: {}
      setpoint: { min: 0, max: 100, unit: "%" }
```

**2. `instance/panel-devices.yaml`** (new side-registry, mirrors `levoit-devices.yaml`):
```yaml
d1001-beachhead:               # MQTT topic prefix the panel actually uses
  device_id: d1001_panel
```

**3. Seed the per-scene brightness policy** (editable later in the PWA policy editor). Either set it in the
PWA once the device shows, or seed via the control API (admin bearer):
```
Home  → brightness 100
Away  → brightness  40
Sleep → brightness   0   (screen off)
```
Policy JSON: `{"enabled": true, "brightness": 100,
  "scenes": {"Home": {"brightness": 100}, "Away": {"brightness": 40}, "Sleep": {"brightness": 0}}}`

**4. Restart the control service** to reload the registry + side-registry:
```
sudo systemctl restart ha-controller     # (whichever unit runs the controller/issuer on .210)
```

## OTA the firmware (bench-built `v60-panel-actuator`)
Panel OTA is the normal D1001 path; if OTA wedges the battery-backed panel, USB `idf.py flash` recovers.

## Live-validate (ADR-0014 R3 + R1/R5)
1. **Direct trait:** publish `d1001-beachhead/cmd/brightness 30` → backlight dims; `…/cmd/brightness 0` → off;
   `…/cmd/screen on` → back on. Confirm retained `d1001-beachhead/state/screen` tracks `{"on","level"}`.
2. **Scene-follower:** `POST /control/house/scene {"scene":"Sleep"}` → panel goes dark; `Away` → 40%; `Home` →
   100%. Verify the issuer log shows `source=scene` and the reported level matches (readback).
3. **PWA (R1/R5):** the panel appears with a switch + brightness control; both admin-gated; a set from the PWA
   reaches the panel and reflects back in `/api/v1/displays`.
