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

Built 2026-10-26: PWA rendering of channels + blower + the comfort warning (`server/api/viewmodel.py`
`trait_now_values` / `comfort_alert`, `server/web/app.js` ManualControl); the Aprilaire→blower follow
(`server/control/follow.py`). Not built: a D1001 alert reader; the comfort clamp (with the automation decision).

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
9. Static DHCP lease on the air-gap router (MAC → `instance/openwrt/airgap_router.env`). **Done 2026-10-25** for the first unit:
   router `uci` host `venstar` → 192.168.1.118 (the host table is hand-set, not in `router_reconcile.py` invariants;
   documented in `provisioning/openwrt/etc/config/dhcp`). Redo with the replacement's MAC.

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
- **4 ✔ 2026-10-26** fan=1 → `fanstate` 1 within 5 s, blower heard (Hugh at the furnace); fan=0 → 0 within 6 s.
- **5 ✔ (caveat) 2026-10-26** heat 72→70 kept cool 74. Our planner refused 73/74 (delta 2); the UNIT accepted a raw
  73/74 POST in Off mode — it does not enforce `setpointdelta` itself there, so the planner is the guard.
- **6 ✔ 2026-10-26** Off→Heat (heat 76 / cool 79): `state` 1, `activestage` 1 within 10 s, Hugh felt heat. A
  wall change (76→72→71) showed in `/query/info` within 10 s — the poll sees wall edits. **Open:** `fanstate` read
  1 from the first sample of the heat call with DIP GAS/EL·O·GAS — either it means "call active" or G is energised
  in heat; Hugh to listen whether the blower starts with the burner or ~30–60 s after it.
- **7 open** — needs the unit's clock set and its schedule on.
- **✔ 2026-10-27 ROOT CAUSE CORRECTED — off-state leakage, not a failed output; fixed with an isolation relay.**
  The "24 V on W" readings below were all taken with W **unloaded** (meter only, furnace W unscrewed). A solid-state
  output leaks a small current when off; into a meter or the furnace's high-impedance W input that reads/acts as
  24 V. With the RIBU1C coil (55 mA) on W the leakage collapses: front on, mode Off → W/Blu and Org low, relay off.
  Heat call on the **original unit** 2026-10-27 01:31:40 PDT → relay LED on, furnace fired (Hugh); 01:32:38 Off →
  LED off, burner stopped. "Before boot" also fits leakage (passive, no firmware needed). RIBU1C at the furnace:
  Wht/Blu (10–30 V coil) ← stat W · Wht/Yel (coil comm) → furnace C · Yel (C) → furnace R · Org (N/O) → furnace W ·
  Wht/Blk (120 V coil) and Blu (N/C) capped separately. Red LED = furnace heat call (a visible indicator).
  Measured: C↔R 28 VAC (normal unloaded 24 V transformer). The section below is the original (wrong) diagnosis,
  kept for the record.
- **❌ (superseded) 2026-10-26 "W OUTPUT FAILED ON (hardware)".** Timeline (UTC): front attached since
  install, furnace quiet. 01:35:48 first heat call (heat 76, via the API); 01:36:20 the call ended (Hugh lowered
  the setpoint at the wall) and the unit reported idle from then on — **but the furnace kept heating** (house
  +3–4 °F, mech closet 94→106 °F by 01:57; furnace status LED steady = 24 V present, no fault). Wall buttons, mode
  Off and every API Off had no effect. Front pulled, then the furnace breaker cycled → stopped. 02:05 front
  re-attached (unit reporting Off/idle) → furnace started at once; front pulled → stopped (blower ran its off-delay).
  Diagnosis (Hugh, meter, 02:20–02:50): W conductor unscrewed at the furnace → **24 VAC W↔Com with the front
  attached in Off; ~3 VAC (stray pickup) with the front removed** (so no wiring short); with the interlock taped,
  **W goes to 24 V the instant the front is plugged in, before the unit boots** — no firmware is running, so it
  is the output stage, not software. Unpowered ohmmeter: R open to every output (a gate/driver-type failure only
  conducts under voltage). DIP verified GAS/EL·O·GAS (photo `T7850_inside`); outputs are three solid-state
  devices (Q1–Q3), no relays. **G output verified good** 02:45:42 on / 02:46:57 off (blower followed).
  Furnace cleared: Carrier 59TP5 gas valve is fed through two series relays (GVR-1/GVR-2) the CPU closes only on a
  W call (schematic photo `59TP5A_user_3`); the furnace behaved correctly throughout. **State now:** W disconnected
  and taped at the furnace (no heat possible); Venstar up for G/Y1/telemetry. Carrier label (photo `59TP5A_user_1`):
  stored status codes erase after 72 h; recall = briefly open one main-limit (24 V) wire per code.
  **Y1 verified good under its real load** 2026-10-26 03:39:30 cool call → outdoor unit ran (Hugh) → 03:42:03
  mode Off → outdoor unit stopped within seconds, blower stopped after the furnace's ~90 s cooling off-delay. So a
  coil load (the contactor) did not kill Y1 on its first switch-off. (Same reason Y1 never misbehaved: the contactor
  coil is a real load, so leakage can't hold it in.) Plan for
  the replacement: RIBU1C isolation relay on W (ordered); a second on Y1 optional; new 18/8 cable with service loops.
  Lessons: (0) **measure a solid-state output under a real load** — an unloaded 24 V reading is not proof it is on;
  (1) a thermostat output can drive a load while the API reports idle — the API is not proof of what the wire
  does → a furnace call monitor (24 V sense on R/W/Y/G + supply-air probe) is the independent check;
  (2) Hugh can't tell burner from blower at the furnace — don't diagnose from "it's still running".
- **8 dry run ✔ 2026-10-26** on ha-2 with a temp registry: both entities, 72 °F → 22.22 °C, cool 74 °F → 23.33 °C,
  thermostat `authoritative:false`.

Re-check procedure for the stance above: `venv/bin/python -m server.ingest.venstar_bridge --once` on ha-2
(prints what the unit reports right now) — to be added to [runbook-device-verification.md](../runbook-device-verification.md)
at go-live.
