# runbook-device-verification.md — how to VERIFY a device's state/function

**Directive: trust but verify** ([AGENTS.md](AGENTS.md) principle 5). A device's state or function is a
*hypothesis* until it has been **verified live** — documented, remembered, or briefly-sampled state is not a
fact. You must verify before relying on a device's state in planning or execution, **even if it costs more
time and tokens.** Any doc that asserts a device's state/stance must link the matching entry here.

This runbook is **durable and separate from the churn**: the design log / checkpoints record *what happened*;
this file records *how to re-check it*. Keep verification procedures here, not inline in the design log.

## How to use
- **Reading a claim about a device?** Find its entry, run the *How to verify* steps, compare to *Expected*.
- **Writing a claim about a device state** (design log, checkpoint, manifest, ADR)? Add/refresh an entry here
  and link it.
- Each entry has four fields: **Claim** (the asserted state) · **How to verify** (exact commands) ·
  **Expected** (what a healthy result looks like) · **Cadence & gotchas** (what makes a naive check lie).

> **Universal gotcha 1 — right topic before absence.** "No data" on a broker watch means nothing if you're on
> the wrong topic tree. ha-2's *scanner* publishes decoded BLE readings to `home/<area>/<device_id>/state`;
> *edge nodes* relay to `home/edge/<node>/<mac>/adv`. Subscribing to one while the data flows on the other reads
> as silence. (This is what first misled the radon investigation.)
> **Universal gotcha 2 — cadence before absence.** Even on the right topic, a sample shorter than the device's
> *republish interval* can legitimately show nothing. Check the expected cadence before concluding silent/dead.

---

## Entry 1 — Radon (`radon_crawlspace`) is recorded on the air-gap (1.0) system

**Claim:** `radon_crawlspace` (Aranet Radon Plus, SAF Tehnika, mfr company `0x0702`, BLE5 *extended*
advertising) is being recorded live on ha-2, and its source is **ha-2's own passive BLE scanner**
(`server/ingest/scanner.py`) — **not** the office-closet W5500 edge node (`hbed_s3`).

**Verified (2026-07-09):**
- Radon is **live**: the newest `radon_crawlspace` row's `ts` advances in step with the rest of the fleet
  (observed `18:22:43Z → 18:27:39Z` across a 5-min window).
- The W5500 (`hbed_s3`) is **NOT** the source: a 300 s capture of `home/edge/hbed_s3/#` saw **0 adv frames**
  (only its `status` heartbeat) while radon advanced — so its Aranet decode is redundant on 1.0, not the path.
- `scanner.py` decodes Aranet `0x0702` ext-adv and publishes decoded readings to `home/<area>/<device_id>/state`
  (a different topic tree than the edge nodes — which is why an edge-topic watch never sees radon).
- **Positive source-ID — CONFIRMED live:** a 5-min watch of `home/+/radon_crawlspace/state` caught the scanner
  publishing **6 fresh frames, one every ~60 s** (`18:30:40…18:34:41Z`), `ts` advancing each time, value moving
  (10→24 Bq/m³ over the session), `meta.mac F4:37:5A:68:9F:1A`, `rssi -86`; `hot.db` advanced in lockstep. The
  W5500 stayed silent throughout. Source = ha-2 `scanner.py`, definitively; `hbed_s3` radon decode is redundant.

**How to verify:**
```bash
# ha-2 (192.168.1.210). Broker creds: instance/mqtt.env (HA_MQTT_USER/PASS).
# 1) Is radon fresh? (ts is an ISO-8601 STRING, not epoch — don't do unixepoch math on it)
sqlite3 instance/db/hot.db \
  "select ts, value from readings where device_id='radon_crawlspace' and metric='radon_bqm3' order by rowid desc limit 3;"
#    → compare newest ts to `select max(ts) from readings;` — within a cadence interval == live.

# 2) Catch the SOURCE publishing (retained value arrives immediately; a fresh one within ~5 min proves live):
set -a; . instance/mqtt.env; set +a
mosquitto_sub -h 127.0.0.1 -u "$HA_MQTT_USER" -P "$HA_MQTT_PASS" -t 'home/+/radon_crawlspace/state' -F '%I | %t | %p' -W 330

# 3) Confirm it is NOT an edge relay (W5500 hbed_s3): should show only `status`, no `/adv`, over 5 min:
mosquitto_sub -h 127.0.0.1 -u "$HA_MQTT_USER" -P "$HA_MQTT_PASS" -t 'home/edge/hbed_s3/#' -F '%I | %t | %p' -W 330

# 4) Sanity: the scanner service is up.
systemctl is-active ha-scanner
```

**Expected:** step 1 → newest radon `ts` within ~one cadence of `now`; step 2 → a `home/crawlspace/radon_crawlspace/state`
message, and `post` DB ts advances during the window; step 3 → `hbed_s3` publishes only `status` (0 `/adv`);
step 4 → `active`.

**Cadence & gotchas:**
- Radon republishes **~60 s** (`scanner.py` `REPUBLISH_INTERVAL_S`; or sooner on a change > `1.0 Bq/m³`) —
  verified: 6 frames in 5 min. A watch under ~90 s can still miss one, so give it ≥2 min on the right topic.
- `ha-scanner` logs **nothing per-reading** (quiet passive scan); an empty `journalctl -u ha-scanner` is
  normal and is **not** evidence it isn't hearing the device. Verify by the published `/state` topic, not logs.
- `hot.db.readings.ts` is a TEXT ISO-8601 string — `datetime(ts,'unixepoch')` / epoch subtraction gives
  nonsense (a ~56-year "age"). Compare ISO strings directly or via `strftime('%s', ts)`.
- ha-2's `instance/hot.db` (no `/db/`) is a 0-byte decoy — the real store is **`instance/db/hot.db`**.

---

## Entry 2 — Shade node (`shades_s3`) runs the verified map and drives the QCT

**Claim:** `shades_s3` (ESP32-S3 inside the Gaposa QCTZ36SDU enclosure, [edge/esp32s3-shades](../edge/esp32s3-shades/README.md))
runs `v4-shades`, and its `kPin[]` map is correct end to end: each of the 18 lines asserts the QCT channel/function the firmware
believes it does, and a signed `shade` command reaches a paired motor.

**Verified (2026-09-27):** `v4-shades` confirmed on `ota_1` via OTA self-test PASS. Channel-order pintest: Hugh observed QCT LEDs
`1U 1S 1D … 6U 6S 6D`, all 18 as predicted. `shade 1 up` → node log `shade: ch1 <- up` → CH1 XS40 moved like a panel tap.
Node was then **powered down** (parked until install), so its retained status reads `offline`. That is expected.

**How to verify** (from `.210`, repo root):
```sh
# 1) Alive + version (retained, arrives at once). -T drops `hello`, which carries the MAC (secret).
timeout 5 mosquitto_sub -h 192.168.1.200 -t 'home/edge/shades_s3/#' -T 'home/edge/shades_s3/hello' -v
# 2) Map: needs a human watching the panel LEDs (and a meter for pads, optional).
python3 tools/shade_cmd.py pintest          # Enter = next; LEDs must read 1U 1S 1D ... 6U 6S 6D
# 3) Path to a motor (ONLY with a paired motor someone is watching):
python3 tools/shade_cmd.py shade 1 up       # node logs `shade: ch1 <- up`; motor moves
python3 tools/shade_cmd.py shade 1 stop
```

**Expected:** (1) `status online ota_X v4-shades` (or later). (2) An unbroken LED sequence in channel order. (3) The node log line plus
visible motion. Before limits are set, the motor is in dead-man mode, so a 500 ms pulse gives only a short turn and stop. That is correct.

**Cadence & gotchas:**
- **The pintest proves the map only as far as a human watched it.** The node cannot see the panel; there is no feedback path (design §3.7).
- **`status offline` while the QCT is powered down is normal.** Check whether the panel is energised before calling it a fault.
- **A `shade` command with no paired, powered motor does nothing visible.** The node log line proves only that the node acted, not the RF.
- Don't pipe `mosquitto_sub` through `grep | head` under `timeout`: grep block-buffers and the kill loses the output, which reads as a dead node.
- OTA "rolled back" on this node: check the image has WiFi first (`README` → Build + deploy). This node's WiFi is compiled in, not in NVS.

## Entry 3 — ERV node (`hvac_c6`) controls the Broan, and the ERV is healthy

**Asserted state:** `hvac_c6` runs `v6-hvac-ctrl` (or later) and is the ERV's bus controller at address
`0x11`. The wall control is **unpowered**. The ERV is fed through `erv_pm` (Tasmota S31, area `attic`).
E50 is a warning that self-clears when the controller returns (design §1.8).

**Verified (2026-10-04):** ERV took the node as controller; polled power 61.5 W vs `erv_pm` 63 W; CFM
92.2/92.5 vs target 92.4; fault register `-1`. OVR boost 63 → 137 W and released on time.

**Re-check:**
```sh
S=$(mktemp)   # write to a file, then read it (grep|head under timeout loses output)
timeout 40 mosquitto_sub -h 192.168.1.200 -v -t 'home/edge/hvac_c6/#' -T 'home/edge/hvac_c6/hello' -t 'home/attic/erv_pm/state' > $S   # -T: hello carries the MAC
grep -E 'status|census|erv/adv' $S | tail -4; grep power_w $S | tail -2
```

**Expected:** (1) `status online ota_X v6-hvac-ctrl` (or later). (2) census `tok_ack` within a few of
`tok_offer` and climbing, `bad` flat. (3) `erv/adv` has `listen_only:false`, `online:true`,
`fault_code:-1`, and `power_w` within ~5 W of `erv_pm`'s `power_w`. (4) `fan_mode_name` matches what was
last set.

**Gotchas:**
- **`tok_ack` frozen while `tok_offer` climbs = nobody is answering** (node not controller, or listen-only
  firmware). The ERV keeps its last mode and shows E50; it does not shut down.
- **If the wall control is powered while the node is controller, both answer `0x11` and collide** — rising
  `bad`, erratic acks. Only one may be live.
- `warning_code` reads `0`, which the firmware currently flags `warning_ok:false` — unconfirmed, not a fault.
- The ERV can be power-cycled remotely via `cmnd/erv_pm/POWER` — no attic trip. It then spends ~2 min in
  start-up (`fan_mode` 20) and settles into LOW, not its previous mode.
- **`fan_mode` 2 = OVR latched** (max airflow, mode writes ignored). Caused by a SHORT OVR pulse; firmware v8
  prevents it (45 s minimum hold). Clear: PWA banner button, or power-cycle via `erv_pm`.

## Entry 4 — ERV automation: air quality, plus the dew-point dry boost

**Asserted state:** `erv_attic`'s policy on ha-2 is enabled, with the air-quality rule (mean AQ of the gas nodes
→ low/med/high) and `dry_boost` set (`outdoor_sensor` `switchbot_outdoor`, five room meters). The ERV goes to
high while the house dew point is ≥ 2 °C above outdoor and the house RH is ≥ 40 % (floor 39 %, Hugh 2026-11-04). Design:
`docs/design/dehumidifier-efficiency-study.md` §7; code `server/control/dry_boost.py`.

**Verified (2026-11-04 19:13 PST):** policy set; the first tick logged `dry boost idle: dp in 7.9 / out 9.1 °C
(Δ -1.2), RH 42 %, coldest room 19.8 °C`. That is correct: outdoor air was wetter than indoors.

**Re-check (on ha-2):**
```sh
cd ~/home_automation && python3 -c "
import sqlite3, json
c = sqlite3.connect('file:instance/db/control.db?mode=ro', uri=True)
print(json.loads(c.execute('select json from automation_policy where device_id=\"erv_attic\"').fetchone()[0]).get('dry_boost'))
for r in c.execute('select ts, reason, act, status from control_log where device_id=\"erv_attic\" order by rowid desc limit 3'): print(r)"
```

**Expected:** (1) the `dry_boost` dict prints with `outdoor_sensor` set. (2) Recent reasons end in
`dry boost idle|waiting|held|ended: …` or `dry boost: … -> level 3` (shown as `high`), each with the dew points.
(3) While it says `dry boost`, the ERV's `fan_mode_name` (Entry 3) is `high`.

**Gotchas:**
- **No `dry boost` text at all** = the policy lost the key, or the controller is running pre-1df7eea code.
- `dry boost off: outdoor reading stale/missing` = `switchbot_outdoor` has been silent > 15 min. It's a BLE
  meter, normally every ~100 s.
- Boost state is in memory: a controller restart drops it, and it re-arms on the next qualifying tick.
- **Wet trim (Hugh 2026-11-10, opt-in, OFF until `wet_trim.step_c` is set):** the mirror of the boost. One level
  lower per `step_c` °C of outdoor-over-indoor dew point, down to `off`; released at once by air quality ≤
  `release_below` (60), indoor RH < `rh_min` (45 %) or stale readings. Code `server/control/wet_trim.py`; sensors
  and Open-Meteo cross-check default to `dry_boost`'s, but a sensor/Open-Meteo disagreement resolves to the DRIER
  reading. Enabled, reasons gain `wet trim idle|held|off: …` or `wet trim −N (step S °C): … -> level L` / `-> off`.
  Disabled = no `wet trim` text at all. Re-check: print `.get('wet_trim')` with the script above.
  **Set 2026-11-10 ~11:40 PST:** `{"enabled": false}` (Hugh: off until the step is chosen). PWA: the ERV's ⚙ Automation
  panel → "Wet trim" (switch, step in °F of dew-point gap, air-quality release, RH minimum, live dew points).
  **Superseded by the dew-point law (below) — its PWA section was replaced by the law's.**
- **Dew-point law (Hugh 2026-11-11, opt-in via `dewpoint_law.enabled`; supersedes the boost and trim while on):**
  one level up per `step_c` of drier outdoor air, one down per step of wetter, clamped off..high; cold outdoor air
  (below `cold_start_c`, one notch per `cold_step_c`) fights the boost, poor indoor air (below `aq_start`, one per
  `aq_step`) fights the trim. Optional `boost_rh_floor`, `trim_rh_floor` (an RH, or `"dehumidifiers"`: trim only
  while RH is above a running `function: dehumidify` device's `off_below`, on its own sensor). Code
  `server/control/dewpoint_law.py`. Enabled, reasons gain `dew-point law ±N: dew-point gap … dry D wet W, cold C (…),
  air A (AQ …)` plus any `no boost:`/`no trim:` guard. Off = no `dew-point law` text and the old boost runs.
  Re-check: print `.get('dewpoint_law')` with the script above; PWA ⚙ Automation → "Dew-point law" shows the last
  decision and every input's age. The "dehumidifiers" floor needs `function: dehumidify` on `dehumidifier_living_room`
  and `dehum_attic` in ha-2's `instance/control.yaml` — re-check with
  `venv/bin/python -c "import yaml;print({k:v.get('function') for k,v in yaml.safe_load(open('instance/control.yaml'))['devices'].items()})"`.

## Entry 4b — Dehumidifier study rotation (Phase 3) is running

**Asserted state:** the controller on ha-2 runs the study rotation (`server/control/study_rotation.py`): 90-min
blocks, arms 0/M/A/MA/E in a random order each cycle, written as ordinary overrides on `dehumidifier_living_room`,
`dehum_attic` and `erv_attic` (expiry = block end + 10 min), one `study_block` row per block. Outside arm E the
ERV runs its air-quality bands with the dry boost/trim/law held. Design: study doc §3D; P3 gate §6c.

**Re-check (on ha-2):**
```sh
cd ~/home_automation && python3 -c "
import sqlite3, json
c = sqlite3.connect('file:instance/db/control.db?mode=ro', uri=True)
print(c.execute('select json from house_setting where key=\"study:rotation\"').fetchone())
print(json.loads(c.execute('select json from house_setting where key=\"study:rotation:state\"').fetchone()[0])['block'])
for r in c.execute('select * from override'): print(r)
for r in c.execute('select id, arm, start_ts, end_ts, end_reason, rh_start from study_block order by id desc limit 4'): print(r)"
```

**Expected:** (1) `enabled: true, paused: false`. (2) A `block` with an arm and an end ~90 min after its start. (3)
Overrides match the arm (M/MA: Midea `boost_on`; A/MA: Aprilaire `boost_on`; E: ERV `boost_on`; the rest `off`, no
ERV row outside E), expiry = block end + 600 s. (4) Recent blocks ended `complete`, or early with `rh_high` /
`rh_low` / `bucket_full`. The PWA top bar shows "🔬 Study: <arm> · Nm left" with a Pause button.

**Gotchas:**
- A block in the `released` list = an operator override on that device (a PWA press). The study stays out of its
  way until the press ends; that's by design, not a fault.
- `gap` end = the controller was down longer than the 10-min grace; the overrides lapsed and the rules ran.
- The ERV's control_log reasons read `… study arm X: dry boost/trim/law held` outside arm E.
- Pause (PWA, or `PUT /api/v1/control/house/study {"paused": true}`) clears the study's overrides at the next tick.

## Entry 4c — Unified control law runs in shadow (logs, never commands)

**Asserted state:** ha-controller on ha-2 runs the law's shadow every 5 min (`server/control/law/shadow.py`),
writing control.db `law_shadow` and `house_setting` `law:last`. No device is enrolled `law`; the law issues no
commands. Design: `docs/design/unified-control-law.md`.

**Re-check (on ha-2):**
```sh
cd ~/home_automation && python3 -c "
import sqlite3, json, time
c = sqlite3.connect('file:instance/db/control.db?mode=ro', uri=True)
l = json.loads(c.execute('select json from house_setting where key=\"law:last\"').fetchone()[0])
print(round(time.time() - l['ts']), 's ago'); print(l['reasons']); print(l['choice'], l['actual'])
print(c.execute('select json from house_setting where key=\"law:enrol\"').fetchone())"
```

**Expected:** (1) the last decision is under ~10 min old. (2) Reasons name the RH, the water need, the AQ and the
chosen combination with its W and cost. (3) `law:enrol` is absent or holds only `rule`/`shadow`. The PWA's
"⚖ Comfort" panel shows the same, plus 24 h agreement per device.

**Gotchas:** the controller logs `law shadow:` lines at INFO every 5 min; `law shadow tick failed` /
`law context failed` with a traceback means the shadow stopped deciding (the rules are unaffected).

## Entry 5 — Smart plugs are switchable from HA

**Asserted state:** six plugs are manual-only switches in ha-2's `instance/control.yaml` (commit 0b909f0):
`plug_hall` (Midea; PG03 on our firmware, node `plug_01`) and the Tasmota S31s `dehum_pm`, `erv_pm`,
`airgap_router_pm`, `failover_pm`, `plug_g11`. Their mode defaults are on/off, except `failover_pm` and
`plug_g11` (the computers), which are power-cycle only. The mode a person selects is stored in control.db as
`power_mode:<id>`. Commands and mode changes need an admin.

**Verified (2026-11-06 ~07:45 PST):** ON sent to `plug_hall` and `dehum_pm` (both already on) returned
`status ok` with `reported {on: true}`. That is the PG03 signed path and the Tasmota path, each confirmed by
the plug's own readback. `/api/v1/displays` listed all six: `On`, a power-cycle control, and
`off_allowed: false` on the two computers.

**Re-check (on ha-2; harmless, it only re-asserts ON):** POST
`/devices/<id>/command {"trait":"switchable","action":"set","args":{"on":true}}` with the admin bearer. Expected:
`200 … "reported":{"on":true}`.

**Gotchas:**
- **`no-ack` from a Tasmota plug** = no `stat/<id>/RESULT` within ~12 s. Usually the S31 is off the network.
- **An Off on `airgap_router_pm` cuts HA's own path to it.** Only its button turns it back on (Hugh chose on/off).
- **Power-cycle is plug-timed.** S31: `Backlog Power 0; Delay <s*10>; Power 1`. PG03: `plug_cycle`. The readback
  confirms the OFF half; the plug restores power by itself.
- `plug_02..08` were offline on 2026-10-31 and aren't registered. Add each as `device_type: plug` when deployed.
