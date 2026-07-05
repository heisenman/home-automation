# E1001 gap register — finish feature analysis + development

**Date:** 2026-07-05  **Status:** Analysis (sign-off gate before dev).  **Owner:** ops.
**Method:** audited the live firmware (`provisioning/reterminal/e1001/`) against ADR-0019 (screen/abstraction),
ADR-0024 (battery standard), and both roadmaps ([e1001-roadmap.md](e1001-roadmap.md),
[e1001-capability-roadmap.md](e1001-capability-roadmap.md), [e1001-epaper-renderer.md](e1001-epaper-renderer.md)).
Reuse-first: every remaining item cites what already exists. Grounds the "finish the E1001" work; nothing is
built until an item is picked up + decomposed.

## Done / conformant (no work)
Platform decision (E0), beachhead (E1), spec-driven ePaper renderer + alerts + scene header + SNTP clock (E2),
deep-sleep + button-wake **mechanism**, **battery profile v1 + ADR-0024 loadable path** (`d910374`), edge presence
Stage 1, recovery-asymmetry (correctly *not* a recovery/BLE-census node), OTA-over-WiFi.

## The backlog

| # | Gap | Verdict | Value / Risk | Owner | Reuse |
|---|-----|---------|--------------|-------|-------|
| 1 | **ADR-0024 safety policy** — hard-off @0%, warn @5–10%, **boot-gate** (hold ePaper dark below cold-start floor) | **Missing** — floors stored/persisted/published (`e1001.yaml:466-485`) but wired to **no action** | **SAFETY-critical** | ops (build); §3 hard-off gated on **B1** | `ha_power_policy` 3-band semantics + cfg fields + D1001 preset as template (re-express in lambda) |
| 2 | State offsets 0/0/0 (display/USB/charging) | **Partial** — gauge un-normalized across power states (`:34-36`) | Med (footer over-reads on wall/charge) | ops; gated on **B2** | `tools/battery_characterize.py` `measure_offsets()` + `e1001_profile.py` fit → **re-push, no reflash** |
| 3 | RTC holdover clock (PCF85063T @0x51) | **Missing** — SNTP-only (`:144`) | **High** (instant time on wake, cheaper wakes — roadmap #1) | ops | ESPHome-native `time: pcf85063` (config-only, no driver) |
| 4 | Buzzer audible alert (MLT-8530 / IO45) | **Missing** | Med (ePaper is silent on critical alerts) | ops | `output: ledc` + `rtttl`; fire on `g_alert_crit`; **must be easy to disable** ([[feedback-audio-easy-disable]]) |
| 5 | Power budget (P4e Step 3) | **Missing** | Med (battery-life number; gates #6) | **Hugh+ops bench** (meter inline) | `battery_characterize.py` pattern |
| 6 | PDM mic acoustic sensing (MSM261D) | **Missing** | Low / novel; power-gated on #5 | Defer | `microphone: i2s_audio` |
| 7 | BLE Stage 2 field-validation | **Partial** — `switchbot_ble` built + wall-gated, unproven in field | Med | Validate at deploy | already wired (`:639-647`) |
| 8 | SHT40 → canonical `home/<area>/e1001/state` | **Partial** — publishes edge adv (`:661-671`); pipe is end-to-end | Med | **GATED → Hugh** | one `instance/devices.yaml` row + restart `ha-edge-mapper` (mapper unchanged) |
| 9 | Enrollment + capability descriptor + deploy (rename `e1001-bench`→`e1001-<area>`) | **Missing/partial** | Med (fleet awareness) | **Partly gated → Hugh** | `enroll_node.py --no-secrets-file`→`secrets.yaml`; `mint_panel_token.py` if it drives scene/ack; **descriptor is ADR-0019 §3 spec-only (no live registry file yet)** → low priority |
| 10 | Gauge + renderer polish | **Partial** | Low | ops | SoC **unsmoothed** (single 60 s ADC sample) → add symmetric/median filter (ADR-0024 §1); metric selection truncated to first-2 graphs; `°` glyph |
| 11 | Production hardening | **Missing** | Med (pre-deploy) | ops | strip/gate bench affordances (`Test Sleep/Alert/Edge`, `Prof Sim Crash`, charger switches); delete dead `components/e1001_ui/` scaffold (uncompiled, no `__init__.py`); fix stale header (`:1` "NO display yet") |
| 12 | ePaper visual QA | **Unverified** | — | **Hugh's eyes** | — |
| 13 | Local SD logging | **Deferred** (low value; dictator owns history) | Low | Skip | — |

## Two docs-first blockers to resolve *first* (cheap; scope #1 and #2)
- **B1 — Can the E1001 firmware trigger a hard power-off?** The D1001 kills its rail via a firmware GPIO
  (`bsp_power_off` → expander `PWR_HOLD` P8). The E1001 latch is `CJ3407` P-FET (Q1) + **physical slide switch
  SW1** + `ESP_RST` — no documented firmware-reachable rail-kill. **Read `docs/hardware/
  reTerminal_E1001_V1_2_SCH_251120.pdf` sheet 6 / ask Hugh.** If physical-only → §3 "hard-off" degrades to
  `deep_sleep.enter` (ePaper holds its last image, no timer wake); **boot-gate (§6) + warn (§4) are implementable
  regardless** and are the highest-safety-value pieces.
- **B2 — Is there a burst telemetry cadence?** ADR-0024 §3 wants 250 ms–1 s sampling for the offset/transition
  capture; the E1001 publishes `e1001-bench/battprofile` at a fixed 5 s. Confirm/add a burst-rate knob before the
  §3 offset run (#2).

## Reuse map (build X → reuse Y)
- **Safety policy (#1):** `firmware/components/ha_power_policy/include/ha_power_policy.h` — pure `eval()` (NONE/
  WARN_ON/WARN_OFF/SHUTDOWN) + `boot_ok()`; cfg fields `shutdown_mv/warn_mv/warn_clear_mv/boot_gate_mv/
  boot_release_mv/shutdown_debounce`; D1001 preset `3450/3520/3580/3550/3650/2` as a **starting template**
  (re-settle via #2's run — deep-sleep load model differs). Wiring reference: `beachhead_main.c`
  (`ha_power_policy_boot_gate` before display bring-up; `ha_power_policy_monitor_start` @5 s). LUT-free → safe
  before any curve. **Re-express in ESPHome lambdas** (cannot link the C module into ESPHome).
- **Characterization (#2):** `tools/battery_characterize.py` `measure_offsets()` = the ADR-0024 §3 step-each-knob
  routine (retarget `d1001-beachhead`→`e1001-bench` topics); `tools/e1001_profile.py e1001lut` + `e1001_capture.sh`
  already E1001-native → fit → `--write-json` re-push (no reflash).
- **RTC (#3):** ESPHome first-party `time: pcf85063`; SNTP writes the RTC, RTC serves time across sleep. D1001
  `ha_rtc` (C) is semantic precedent only.
- **SHT40 (#8):** publisher built (`e1001.yaml:661`); `server/ingest/edge_mapper.py:204` republishes canonical
  `home/{area}/{device_id}/state` unchanged — add one `instance/devices.yaml` row `e1001-sht40 → {device_id:e1001,
  area:<room>, device_type:sht40}` (gated write → hand Hugh).
- **Enrollment (#9):** `tools/enroll_node.py` (LUT-only via `--no-secrets-file`, then creds into `secrets.yaml`);
  `tools/mint_panel_token.py` if it issues scene/ack. Capability descriptor = ADR-0019 §3 `hallway-e1001` template
  (spec-only today).

## Implementation procedure
Each step: **decompose → reuse → build → validate**, OTA-able and independently shippable; decompose before code
([[feedback-decompose-before-dev]]); every hardware/protocol fact doc-grounded ([[feedback-docs-first]]).

### Step 0 — Resolve the two docs-first blockers (no code)
- **B1:** read `docs/hardware/reTerminal_E1001_V1_2_SCH_251120.pdf` sheet 6, trace Q1 (CJ3407) gate — is it
  driven by an ESP GPIO (firmware rail-kill, like the D1001) or only by SW1 / ESP_RST (physical-only)? Unclear →
  ask Hugh (EE). **Output:** hard-off = {GPIO-reachable | physical-only → degrade to `deep_sleep.enter`}.
- **B2:** inspect the `interval:` telemetry cadence in `e1001.yaml` (fixed 5 s today) — add a runtime burst-rate
  number (250 ms–1 s) for the offset capture, or a temporary override. Small config add.

### Step 1 — ADR-0024 safety policy (#1)  [after B0]
- **Decompose (ESPHome lambdas mirroring `ha_power_policy`; cannot link the C module):**
  (A) read+normalize cell mV (reuse the footer normalize + the smoothed value from Step 5);
  (B) 3-band decision on an `interval: ~5s` lambda with globals `g_pp_low_streak`/`g_pp_warned`, thresholds from
  the already-stored `g_run_floor_mv`/`g_warn_mv`/`g_warn_clear_mv` (+ a debounce count) → emits WARN_ON/OFF/SHUTDOWN;
  (C) **boot-gate**: an early `on_boot` (before the pri-100 ePaper draw) reads cell mV; if `< g_boot_gate_mv` hold
  the display dark + blink `status_led` (the low-power channel), poll until `>= g_boot_release_mv` (hysteresis), then draw;
  (D) actuators — warn = inject a synthetic critical alert into the banner (as `Test Alert` does) + buzzer (Step 3) +
  optional MQTT/ntfy; shutdown = per **B1** (GPIO rail-kill, else `deep_sleep.enter` = degraded off); led = `status_led`.
- **Reuse:** `ha_power_policy.h` 3-band semantics + cfg fields; D1001 preset `3450/3520/3580/3550/3650/2` as the
  starting template (re-settle after Step 4).
- **Build:** ship boot-gate + warn first (unblocked); add hard-off once B1 is known.
- **Validate:** set warn/shutdown thresholds *above* current V (existing `Prof V Floor`-style knob) to trip instantly
  → confirm banner + LED + shutdown; set `boot_gate` above V, reboot → ePaper stays dark + LED blinks → lower it → draws.

### Step 2 — RTC holdover clock (#3)
- **Decompose:** add `time: platform: pcf85063` (addr `0x51`, `i2c_id: bus_i2c0`) as a second source; on SNTP sync →
  write the RTC; on boot/wake → read the RTC *before* WiFi so the first ePaper frame has the clock; renderer uses RTC
  when valid, else SNTP.
- **Reuse:** first-party ESPHome `pcf85063` platform (no driver). Datasheet `docs/hardware/PCF85063TP.pdf`.
- **Validate:** block WiFi/NTP → clock still right from the RTC; pull the battery briefly → time survives (coin cell).

### Step 3 — Buzzer audible alert (#4)
- **Decompose:** `output: ledc` on IO45 (passive buzzer needs PWM tone, not a plain GPIO; via Q7 `BUZZER_EN`) +
  `rtttl:`; fire a short chime when `g_alert_crit` goes true in the fetch/render path. **Easy-disable**
  ([[feedback-audio-easy-disable]]): a persisted `Buzzer Enable` switch (mirror `Sleep Enable`), default off-able over MQTT.
- **Validate:** `Test Alert` (critical) → chime; `Buzzer Enable` OFF → silent. Docs-first: confirm Q7 gate polarity + MLT-8530 passive drive.

### Step 4 — State-offset characterization (#2)  [needs B2]
- **Procedure:** with the burst cadence, run `tools/battery_characterize.py measure_offsets` retargeted to `e1001-bench`
  — step display on/off, USB in/out (manual prompt), charge on/off (`set_charge_enable`), record each Δmv at a known SoC.
  Set `off_display_off/usb/charging` in `battery_profile_v1.json`, bump version → **push (no reflash)**. Apply the
  display-off offset in the footer only if a sleep-time read path is added (omitting it now is correct — base frame is display-on).
- **Validate:** footer % no longer jumps between on-battery and on-wall/charging.

### Step 5 — Gauge smoothing + renderer polish + production hardening (#10/#11)
- **Gauge:** add a `median`/`sliding_window_moving_average` filter on `batt_v` (ADR-0024 §1 — no one-way smoothing).
- **Renderer:** make metric selection fully spec-driven (lift the first-2 cap or make it a spec field); add the `°` glyph.
- **Hardening:** gate/remove bench affordances (`Test Sleep/Alert/Edge`, `Prof Sim Crash`, charger switches) behind a dev flag
  or drop for production; delete the dead `components/e1001_ui/` scaffold; fix the stale header (`e1001.yaml:1`).
- **Validate:** % stable under a load step; all spec metrics render; bench controls gone.

### Step 6 — Deploy prep (#8/#9)  [gated → Hugh]
- Rename substitutions `device_name`/`edge_node` `e1001-bench`→`e1001-<area>` (+ friendly_name / AP SSID); OTA.
- **SHT40 (gated):** hand Hugh the `instance/devices.yaml` row `e1001-sht40 → {device_id:e1001, area:<room>,
  device_type:sht40, capabilities:[temperature,humidity]}` + `systemctl restart ha-edge-mapper`; verify canonical
  `home/<area>/e1001/state` appears.
- **Enrollment (gated, if it issues scene/ack or for fleet id):** hand Hugh `enroll_node.py --node-id e1001 --mac
  <eFuse MAC> --no-secrets-file`, creds into `secrets.yaml`; capability descriptor per ADR-0019 §3 (spec-only, low-pri).
- Mount/power; enable deep-sleep (`Sleep Enable` ON) + validate the wake cycle.

### Step 7 — Power-budget bench (#5) → then mic (#6)  [Hugh + bench]
- Meter inline on the battery lead: sleep µA + per-wake charge → battery-life quote; then scope the PDM mic
  (`microphone: i2s_audio`, duty-cycled/wall-gated) against that budget.

### Continuous (not sequenced)
- **ePaper visual QA (#12)** — Hugh confirms scene header / clock / footer layout on-screen (I can't see it).
- **BLE Stage 2 field-validation (#7)** — with a SwitchBot in range at the deploy location.

## Gated hand-offs (never self-deploy — hand Hugh copy-paste)
- `instance/devices.yaml` SHT40 row + `systemctl restart ha-edge-mapper` (#8).
- `enroll_node.py` LUT write + any operator-token mint on the dictator (#9).

## Gated hand-offs (never self-deploy — hand Hugh copy-paste)
- `instance/devices.yaml` SHT40 row + `systemctl restart ha-edge-mapper` (#8).
- `enroll_node.py` LUT write + any operator-token mint on the dictator (#9).
