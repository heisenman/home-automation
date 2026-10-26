# Venstar ColorTouch T7850 — device intake (ADR-0043)

Intake per [DEVICE-INTAKE.md](../DEVICE-INTAKE.md). Status 2026-10-26: **installed and on `autohome_airgap` (192.168.1.118), local API on** — read-only checks
(§5 1, 2, 8 dry run) passed; commands, schedule, lease and the bridge unit still open.

## 1. The system it replaces (Stage 0, from photos hvac_1–4, 2026-10-24)

| Item | Fact | Source |
|---|---|---|
| Furnace | Carrier **59TP5A060** — two-stage gas, variable-speed blower | rating label (hvac_1) |
| Furnace board terminals | R (2 red) · W/W1 (white) · Com 24V (2 blue) · G (green) · Y1 (2 yellow) · **Y/Y2, DHUM, W2 empty** | hvac_2 close-up |
| Outdoor unit | single-stage AC (only Y1 used; second yellow + blue go to it) | hvac_2 |
| Old thermostat | Honeywell RTH6580WF, cloud-only; wired Rc(+R jumper) · C · W · Y · G | hvac_4 |
| Spare conductors | several folded back in the wall (≥ green, white, dark, orange — count unverified) | hvac_4 |
| Unknown | what the **second red on the furnace R** powers (accessory / condensate switch?) — check before rewiring | hvac_2 |

Swap: same five wires to R · C · W1 · Y1 · G; configure conventional, 1 heat / 1 cool, fan in heat = furnace
controls (gas). Optional later: a spare conductor to **W2** so the system, not the furnace's timer, picks high fire.

## 2. Local API (Stage 0 — the authoritative doc)

Venstar's published spec, `https://developer.venstar.com/documentation/` (API v4, fetched 2026-10-24), plus the
field-proven `venstarcolortouch` client (Home Assistant's) for what the spec omits.

- `GET /` → `api_ver`, `type` (residential|commercial), `model`, `firmware`.
- `GET /query/info` → `mode` 0 off|1 heat|2 cool|3 auto · `state` 0 idle|1 heating|2 cooling|3 lockout|4 error ·
  `activestage` · `fan` 0 auto|1 on · `fanstate` · `tempunits` 0 °F|1 °C · `schedule` · `schedulepart` · `away` ·
  `spacetemp` · `heattemp` / `cooltemp` · `heattempmin/max`, `cooltempmin/max` · `setpointdelta` · `hum`
  (T7850: no humidity sensor — expect 0/absent) · `availablemodes` 0 all|1 heat/cool|2 heat|3 cool.
- `GET /query/sensors | /runtimes | /alerts` — remote/outdoor sensors, daily stage runtimes, filter/service alerts.
- `POST /control` (form-encoded): `mode`, `fan`, `heattemp`, `cooltemp`, `pin`. **Any call with `mode` must carry
  both setpoints**; auto needs `cooltemp − heattemp ≥ setpointdelta`. A **fan-only** call is valid (the HA client
  sends `fan=` alone). Reply is HTTP 200 either way — `{"success":true}` or `{"error":true,"reason":…}`.
- `POST /settings`: `tempunits`, `away`, `schedule`, `hum_setpoint`, `dehum_setpoint`, `pin`.
- Auth: the spec says nothing; the unit's API menu offers HTTPS + username/password — **HTTP digest** in the HA
  client. Screen lock adds a `pin` to every POST. Discovery: SSDP `venstar:thermostat:ecp` (not used — static lease).

## 3. Abilities and obligations (Stages 1–2, [CONFORMANCE.md](../CONFORMANCE.md))

| Ability | Will exercise | Binds / how we meet it |
|---|---|---|
| **A** senses | temperature (+ `hum` if any) | own sensor = **non-authoritative** (bridge sets `meta.authoritative=false`); never an automatic control source |
| **B** actuator traits | `mode`, `setpoint` (heat/cool channels), `switchable` (blower) | trait vocabulary ✔ · R1 surface every trait — **PWA/D1001 channel rendering is a go-live item** · R3 verify ranges live (§5) · R5 admin-gated via the existing command path ✔ · R6 interlocks: the unit's own (short-cycle, setpointdelta) |
| C signed directives | **no** — a vendor LAN appliance, like the Midea/Levoit; the issuer authorises, the network is the boundary | — |
| J comms events | via the issuer's normal audit/ack path | ✔ |
| Could-do backlog | runtimes → furnace/AC duty-cycle history · filter alert → PWA · W2 staging · remote sensor in another room | |

## 4. Decomposition (Stage 4) — what exists, what's new

| Piece | Where | Reuse / new |
|---|---|---|
| HTTP client + pure planners (`plan_mode`, `plan_setpoint`, `plan_fan`, `info_metrics`) | `server/control/venstar_api.py` | new, stdlib only (urllib digest) — no new venv package |
| issuer Transport (two entities) | `server/control/venstar_driver.py` | same seam as `levoit_driver` / `midea_driver` (ADR-0012 resource manager) |
| routing | `server/control/bootstrap.py` | by `device_type` (ADR-0026), creds from `instance/venstar.env` |
| telemetry | `server/ingest/venstar_bridge.py` + `systemd/ha-venstar-bridge.service` | sibling of `tasmota_bridge` (canonical `/state`) — **unit not installed** |
| setpoint channels | `server/control/traits.py` | extension of the existing trait, backward compatible |
| tests | `tests/test_venstar.py` | fake unit enforcing the spec's rules |

Not built yet: PWA/D1001 rendering of channels + the out-of-limits warning; the comfort-limit clamp (with the
automation decision); the Aprilaire→blower coupling.

## 5. Install-day verification (Stage 6 — nothing above is trusted until these pass)

Record each result with the date in this section.

1. `GET /` answers; `model` = T7850, note `api_ver` + `firmware`.
2. `GET /query/info`: `tempunits` (expect 0 °F — the trait unit must match), `availablemodes` (expect 0),
   `setpointdelta`, the four min/max limits, `hum` (absent/0?), `schedule` = 1.
3. Auth: with HTTPS + user/pass on, an unauthenticated GET is refused and digest succeeds.
4. Fan-only `POST fan=1` → `fanstate` 1 within a poll, blower audibly on; `fan=0` → off when idle.
5. Heat setpoint change keeps the cool setpoint; a crowding one is refused (`setpointdelta`).
6. Mode change round-trips (Off ↔ Heat) with both setpoints carried.
7. Schedule interaction: after a setpoint POST, does the unit hold it until the next schedule period (expected)
   or drop the schedule? Record — it decides how overrides must be shaped.
8. Bridge `--once` prints both entities with sane °C values; then install the unit (gated).
9. Static DHCP lease on the air-gap router (MAC → `instance/openwrt/airgap_router.env`, `router_reconcile.py`).

### Results

- **2026-10-26 wiring** (photo `T7850_mounted`): R (old Rc; one transformer — the T7850 has a single R, no Rc/Rh)
  · C · W → W1/O/B · Y → Y1 · G. W2/W3/Y2/SENSOR empty. Per manual p.75–76: DIP GAS/EL + GAS; 1 heat / 1 compressor stage.
- **1 ✔ 2026-10-26** `GET /` → `api_ver` **9** (spec doc is v4; v4 fields all present), `model` **"COLORTOUCH"** (not
  "T7850"), `firmware` 6.93.
- **2 ✔ 2026-10-26** `tempunits` 0 (°F, matches the trait) · `availablemodes` 0 · `setpointdelta` 2 · heat/cool
  min/max 35/99 · **no `hum` key** (absent, as the bridge assumes) · `schedule` **0** (off out of the box — ADR-0043
  §3 wants it on) · mode 0 off. `/query/sensors`, `/query/runtimes`, `/query/alerts` all answer (alerts: Air Filter,
  UV Lamp, Service). `/query/runtimes` `ts` = 2019-09-01 → **the unit's clock looks unset** (no internet/NTP on the
  air gap) — set date/time on the wall before turning the schedule on.
- **3 — n/a**: Hugh chose HTTP with no login (air-gap posture). The client handles Basic or Digest if that changes.
- **8 dry run ✔ 2026-10-26** on ha-2 with a temp registry: both entities, 72 °F → 22.22 °C, cool 74 °F → 23.33 °C,
  thermostat `authoritative:false`.

Re-check procedure for the stance above: `venv/bin/python -m server.ingest.venstar_bridge --once` on ha-2
(prints what the unit reports right now) — to be added to [runbook-device-verification.md](../runbook-device-verification.md)
at go-live.
