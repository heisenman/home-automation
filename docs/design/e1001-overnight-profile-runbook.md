# E1001 overnight battery profile + offset run — runbook

**Goal (unattended, overnight):** bake the battery gauge's missing pieces into a pushed v2 profile —
(1) USB/charging **voltage offsets**, (2) a fresh **V→SoC LUT** + OCV anchors + a real **mAh capacity /
battery-life** number — with **no reflash** and **no physical action** (the panel is plugged in; the onboard
charger's HIZ lever replaces the USB unplug). Resolves gap-register **Step 4** (offsets) + **Step 5** (power
budget). Tool: `tools/e1001_overnight_profile.py`. Node `e1001-c-office`, on VIP `.1.200` (→ ha-2).

## Why it's safe to run unattended (all firmware-side, verified in `e1001.yaml`)
- **Charger hardware watchdog (~40s):** during a cycle the firmware kicks it every 2s (`e1001.yaml:399`);
  if the firmware hangs, the script dies, the network drops, or ha-2 reboots, the charger IC **auto-reverts
  to charging within ~40s** — a hardware dead-man independent of WiFi/MQTT/the script. (The panel's
  `Prof Sim Crash` control exists to validate exactly this.)
- **Floor-gate:** discharge stops at `Prof V Floor` — we set **3.70 V (~20% SoC)**, well above the 3.45 V
  brown-out (`e1001.yaml:400-408`).
- **Load only in DISCHARGE:** the drain load can't run at rest/charge (`:384-388`); auto-safe on done (`:440`).
- **Script guards (redundant):** a voltage watchdog aborts to safe if V < 3.62 V or telemetry goes dark,
  a time cap, and a `finally:` that always issues `Prof Stop` (→ HIZ off, charge on).
- **Control verified LIVE 2026-07-10:** HIZ ON→`hiz:true`, OFF→`hiz:false`, charge_enable toggles — round-trip
  against the real panel, ended safe.

## Sequence
1. **Phase A — offsets (~10 min):** step-each-knob via the charger switches — base (battery-only, HIZ on) →
   USB-present (HIZ off, charge off) → charging (charge on); `off_usb_mv`/`off_charging_mv` = median-V deltas;
   display offset = 0 (e-ink). Offsets are ~SoC-independent, so one capture near-full is representative.
2. **Phase B — firmware profiler cycle (overnight):** set floor 3.70 V, 1 cycle, rest 120 s, **load level 2**
   (WiFi-PA drain so a full discharge→charge finishes overnight), press `Prof Start Cycle`; log `battprofile`
   every 5 s to `~/e1001-profile-<stamp>.jsonl`. Firmware runs DISCHARGE→REST_D(OCV_d)→CHARGE(mAh)→REST_C(OCV_c)
   →safe. Script supervises with the voltage watchdog.
3. **Phase C — morning (fit + push, no reflash):**
   ```sh
   # on ha-2, once the cycle completed:
   venv/bin/python tools/e1001_profile.py e1001lut ~/e1001-profile-<stamp>.jsonl \
       --write-json provisioning/reterminal/e1001/battery_profile_v1.json --version v2 --date <today>
   # edit off_usb_mv / off_charging_mv into that JSON from ~/e1001-offsets-<stamp>.json, then push:
   venv/bin/python tools/d1001_profile_push.py --broker 127.0.0.1 --node e1001-c-office \
       --from-json provisioning/reterminal/e1001/battery_profile_v1.json --push
   venv/bin/python tools/d1001_profile_push.py --broker 127.0.0.1 --node e1001-c-office --get   # verify source=pushed
   ```
   Push is validated by the device (rejects a non-ascending LUT) and revertible (`--default`). Commit the v2 JSON.

## Launch (on ha-2)
```sh
cd ~/home_automation
nohup venv/bin/python tools/e1001_overnight_profile.py --broker 127.0.0.1 --run \
    > ~/e1001-profile-run.log 2>&1 &
```
Watch: `tail -f ~/e1001-profile-run.log`. Abort anytime: publish `e1001-c-office/button/prof_stop/command PRESS`
(or the script's `finally` / the 40 s HW watchdog handles it).

## Knobs (defaults chosen for a safe, complete overnight run)
`--floor-v 3.70` (discharge stop) · `--hard-floor-v 3.62` (redundant abort) · `--load 2` (finish overnight;
drop to 1 for gentler/partial) · `--cycles 1` · `--rest-s 120` · `--settle 120` · `--max-hours 9`.
