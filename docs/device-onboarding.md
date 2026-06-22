# Device onboarding playbook (users + LLMs)

How to bring a new device into the system, expose its capabilities, and bind it to the right data —
following the standing rules in [ADR-0014](adr/ADR-0014-device-control-conventions.md). Two device
classes:

- **Sensors** — telemetry *in* (SwitchBot BLE meters, Aranet radon). They flow to storage and appear in
  the dashboard/graphs automatically once registered.
- **Actuators** — commands *out* (Midea dehumidifier). They need a driver, declared capabilities
  (traits), a confirmed sensor binding, and they light up the control UI from their traits.

## Where things live (the map)

| Concern | File |
| --- | --- |
| Sensor identity (MAC → device_id/area/type) | `instance/devices.yaml` (gitignored) |
| Actuator registry (node/area/**traits**) | `instance/control.yaml` (gitignored) |
| Per-device secrets (HMAC, tokens) | `instance/control_secrets.yaml`, `instance/*.env` (gitignored, chmod 600) |
| Sensor ingest (BLE adv / history / CSV) | `server/ingest/`, `server/storage/writer.py` |
| Actuator driver + trait→command map | `server/control/<device>_driver.py` (e.g. `midea_driver.py`) |
| Command authorization + signing | `server/control/issuer.py`, `policy.py`, `protocol.py` |
| Control loop (sensor→resolver→actuator) | `server/control/controller.py`, `automation.py` |
| Read/control/BFF API | `server/api/main.py`, `server/api/control.py`, `server/api/viewmodel.py` |
| Web app (auto-renders controls from traits) | `server/web/app.js` |

## Onboarding a SENSOR

1. **Register identity** in `instance/devices.yaml`: `MAC → device_id`, `area`, `device_type`. The
   writer maps incoming readings to this and persists to `hot.db`.
2. **Disambiguate identity if unsure — the breathe test.** Physically perturb the suspect meter (breathe
   on it / warm it) and watch which MAC's humidity/temp spikes in real time. MAC↔identity proven this way
   is golden (it's how the c_office/living_room swap was caught). Record it in the registry comment.
3. **Trust tags.** Readings are transport-tagged (`ble-adv`, `ble-history`, `csv-import`); app-exported
   CSV is trusted/app-labeled. Keep `authoritative=1` for real sensors; only device self-reports are `0`.
4. **Done — it's automatic.** The sensor now appears in `/devices`, `/api/v1/sensors`, the dashboard
   Sensors section, and is graphable + selectable as a control source. No UI code needed.

## Onboarding an ACTUATOR

Order matters (ADR-0014 R7). Using the Midea dehumidifier as the worked example:

1. **Get LOCAL control first.** Many cloud appliances need a one-time cloud handshake to extract local
   credentials, then run fully on-LAN. The Midea recipe (NetHome Plus account → `msmart-ng` token+key
   extraction → `midea-beautiful-air` local control) is in the `midea-dehumidifier` memory + the
   `instance/research/` scratch. Goal: a fully-local `status()` / `set()` path, no cloud at runtime.
2. **Write a driver + Transport** (`server/control/<device>_driver.py`): a `Driver` wrapping the local
   protocol (`status()` returns normalized fields incl. interlocks; `set(**flags)` actuates), and a
   `Transport` that maps our **traits** → the device's command flags and reports state back for
   closed-loop reconciliation. Make the runner injectable so it unit-tests with no hardware.
3. **Declare it in `instance/control.yaml`**: `node`, `area`, and `traits` with ranges, e.g.
   `switchable {safe_on: false}`, `setpoint {min, max, safe_value}`, `ranged {min, max, step}`.
   The trait vocabulary (switchable / setpoint / ranged / lockable / positionable) is the contract the
   issuer enforces and the UI renders from.
4. **Secrets** → gitignored `instance/` (per-device HMAC in `control_secrets.yaml`; tokens in `*.env`,
   chmod 600). Never in git, never in logs/transcripts.
5. **VERIFY capabilities live (ADR-0014 R3).** Device-advertised capabilities are a *hypothesis*. Issue
   each command and compare `reported` vs `intended` (the issuer returns both); confirm physically
   (ear/eye) for anything audible/visible. Pin verified ranges in `control.yaml` with a dated
   `# verified live` note. *Precedent:* the dehumidifier advertised 3 fan speeds but accepts only
   Low=40/High=80 — caught because `reported` disagreed with `intended`.
6. **Confirm the sensor source binding with the user (ADR-0014 R2).** ASK which sensor(s) are valid
   control inputs — never auto-wire, never default to the device's own (off-by-12%) sensor. The binding
   becomes user-editable (the humidity-source dropdown). Seed it only after the user confirms.
7. **Confirm UI exposure (ADR-0014 R1).** The view-model carries `traits` + live `actuator` values
   (`app.state.control_registry` → `build_display`); the web app renders manual controls from them
   automatically. Verify each capability shows a control and actuates.
8. **Seed a default policy** in `control.db` (the controller seeds one on first run); the user edits
   thresholds / source / quiet-window / strategy via the Settings panel (`PUT /control/{id}/policy`).

## Capability → UI mapping

| Trait | Config | Control rendered | Command |
| --- | --- | --- | --- |
| `switchable` | `{safe_on}` | Override Off/Boost (automation-aware) | `{trait:switchable,args:{on}}` |
| `setpoint` | `{min,max,safe_value}` | number input + Set | `{trait:setpoint,args:{value}}` |
| `ranged` | `{min,max,step}` | discrete buttons (Low/Med/High or values) | `{trait:ranged,args:{level}}` |
| `lockable` | `{}` | lock/unlock (needs confirm token) | `{trait:lockable,action:lock/unlock}` |

## Verification checklist (a device isn't "done" until all true)

- [ ] Registry entry: id / area / device_type
- [ ] Friendly name shows in the UI
- [ ] Every trait verified live (reported == intended; physical confirm where applicable)
- [ ] `control.yaml` ranges match real hardware, dated `# verified live`
- [ ] Sensor source binding **confirmed by the user**; device self-report excluded unless elected
- [ ] Secrets in gitignored `instance/`, chmod 600
- [ ] Each capability renders a working control in the web app
- [ ] Default policy seeded; thresholds/source/schedule editable
- [ ] Admin gate enforced (401 without bearer); auth verified at unlock

## Quick smoke commands (on `.245`)

```bash
# read views
curl -s localhost:8123/api/v1/sensors            # all trusted sensors + latest values
curl -s localhost:8123/api/v1/display/<device>   # one actuator's view-model (traits + actuator + health)
# auth gate
curl -so/dev/null -w '%{http_code}\n' localhost:8123/control/auth/check          # 401 (no bearer)
# admin command (token derived on-box, never echoed)
TOK=$(venv/bin/python -c 'import hashlib,pathlib;m=pathlib.Path("instance/.master_pass").read_text().strip();print(hashlib.sha256(("ha-api:"+m).encode()).hexdigest())')
curl -s -X POST -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"trait":"setpoint","action":"set","args":{"value":55}}' localhost:8123/devices/<device>/command
# tests
venv/bin/python -m tests.run_all
```
