# ADR-0042 — Smart plugs as edge nodes, taken over from Tasmota over the air

**Status:** Accepted — **first unit taken over live 2026-10-19 03:18Z** (`plug_01`, over WiFi, no case opened):
partition table migrated (boots slot `ota_0`), meter reading (119.7 V), signed `plug_set`/`plug_status` verified.
**All 8 taken over 2026-10-19** (plug_02..08 by `tools/plug_line.py`, v3, each first-pass after the retry fix;
plug_01 meters the Midea as `plug_hall`). **2026-10-19 04:01Z: plug_01 v1 → v3 by signed OTA from ha-2 on
Tasmota's bootloader** (ota_0 → ota_1, hash + identity verified, self-test confirmed) **with the relay held**: the
Midea on it kept drawing its idle ~1.3 W straight through the reboot — no ~32 W power-up fan run, which the same
Midea showed when it was actually unplugged (03:29Z). (A Midea "implausible status frame" at 04:02:00 is its
normal noise — 82 in 48 h.) Pending: control driver; generic image (§3).
**Relates to:** ADR-0001 (dumb relay), ADR-0010 (signed commands), ADR-0020 (shared core, module-first),
ADR-0036 (node-born secret + standby intake), ADR-0041 (actuators; `verify_power`)

## Context

Hugh bought 8 × IoTorero (Athom) **PG03V3-US16A** plugs (2026-10-18), the `-TAS` variant: an **ESP32-C3, 4 MB**
board shipping **stock Tasmota** (the unit's setup AP `tasmota-XXXXXX-NNNN` confirmed it). Today's metering
plugs (S31s: `erv_pm`, `dehum_pm`, …) run Tasmota under `ha-tasmota-bridge` — unsigned, unmanaged, a second
firmware family. Asked for: bespoke firmware, flashed without opening the plugs if possible, then a production
line for the rest.

### Hardware (Athom's published config `athom-tech/esp32-configs/athom-smart-plug.yaml`; re-check live at intake)

| Function | Pin | Notes |
|---|---|---|
| Relay | GPIO5 | active-high |
| Button | GPIO3 | active-low, pull-up |
| LED (blue) | GPIO6 | active-low |
| CSE7766 power meter | GPIO20 | chip TX → our RX, 4800 baud **8E1**, 24-byte frame every ~50 ms |

GPIO20 is the C3's **UART0 RX** — the default console pin. The console moves to USB-Serial-JTAG and the meter is
read on UART1 with RX routed to GPIO20.

## Decision

### 1. The plug is an edge node (`edge/esp32c3-plug`), not a Tasmota/ESPHome device

Reuses the fleet core unchanged — `ha_config` (identity gate, node-born secret), `ha_wifi`, `ha_sntp`,
`ha_mqtt` (signed commands, hello/standby intake), `ha_ota` (identity-gated signed OTA from ha-2). New modules,
each pure + host-tested: **`ha_cse7766`** (frame parser; algorithm per ESPHome's component) and **`ha_partmig`**
(§2). Telemetry: `home/edge/<node>/plug/adv`, reg `<node>-plug`, metrics named as the Tasmota bridge names them
(`relay_on, power_w, voltage_v, current_a, apparent_va, power_factor, energy_kwh`) + `tripped`, `meter_ok` —
so `verify_power` and the dashboards take a plug in place of an S31 unchanged. The plug reports only the
lifetime `energy_kwh`; the S31's on-device "Today" is **derived on the dictator** as `energy_today_kwh` (house-local
midnight, a reboot rewind adds nothing — `server/storage/energy_today.py`, called by the writer; `--backfill
--parquet --reroll` filled plug_hall's history from 10-19 on 2026-10-24). 30 s heartbeat, immediate on relay
change, and on a power step (> max(5 W, 10 %), ≥ 2 s apart). `power_w` is the mean since the last publish.
Commands (signed): `plug_set {on}`, `plug_cycle {off_s 1..120}` (the ERV OVR-clear power-cycle),
`plug_cfg {boot on|off|restore, lock}`, `plug_status`.

**The relay's safe state is "keep doing what it was doing"** — the opposite of ADR-0041's fail-safe-OFF
(`ha_dout`, deliberately not reused). These plugs feed the ERV and the Aprilaire; their power must not blink
because the plug rebooted or took an OTA. The relay pad is **held** (`gpio_hold_en` — IDF documents the hold
surviving chip/system resets) and mirrored in RTC memory; a warm boot re-asserts it without a pulse. Power-on
follows `boot` (default `restore`, ON the first time). Local decisions, both ones a server can't take in time:
the **button** (short press toggles; ignored under child lock) and an **over-current trip** (> 16 A for ~1 s →
OFF, latched, reported; an explicit ON re-arms). The LED is **dark when healthy** (1 Hz = broker unreachable,
fast = tripped) — the Levoit night-flash lesson.

### 2. Take over from Tasmota over the air: our ota_0 sits on Tasmota's app0 (`ha_partmig`)

Tasmota ≥ 12's 4 MB layout — **read live from our unit (Tasmota 14.3.0, IDF 5.3.1)**: `nvs 0x9000 |
otadata 0xE000 | safeboot (factory) 0x10000 (832 K) | app0 0xE0000 (2880 K) | spiffs 0x3B0000 (320 K)`.
**An upload only reaches app0 from SAFEBOOT:** the running main firmware writes uploads into the 832 K safeboot
slot and refuses a ~1 MB image ("Not enough space" — harmless, nothing written). The web page's own sequence,
reproduced by hand: `GET /u4?u4=fct&api=` (restart into safeboot; poll until it returns `true`), then
`POST /u2?fsz=<bytes>` with the image as form field `u2`. Safeboot writes app0 and boots it. Our
table (`partitions.csv`) keeps nvs + otadata where Tasmota has them and puts **ota_0 at 0xE0000**, so the
uploaded image is already in our slot. On first boot `ha_partmig` sees a table with a `safeboot` app, checks
it is running from that table's app slot and that the slot is our ota_0, then:

1. erase NVS (Tasmota's settings) → 2. erase otadata → 3. erase + write + read-back-verify the 3 KB table at
0x8000 → 4. restart. With otadata blank and no factory partition the bootloader boots ota_0 (verified in IDF's
`bootloader_utility.c`; it also walks to any valid slot). **Only step 3 (~50 ms) is unrecoverable** if power
drops; a cut after step 2 boots Tasmota's safeboot (re-upload). No code is copied, so there is no
self-overwrite. Serial-flashed units get the same table, so every plug has one layout.

Prior art: `elohmeier/tasmota-migrate` (ESPHome) copies itself into the safeboot area and installs ESPHome's
table — a self-copy we avoid by choosing our layout. ESPHome's own guide warns a naive OTA from Tasmota ≥ 12
can soft-brick; that is the layout mismatch this design removes.

### 3. Identity: first unit branded, production line generic

Unit 1 gets a **branded** image (compile-time secret via `enroll_node`, the path proven on every node so far),
so the only new risk on it is §2. The production line should be **one generic image** (ADR-0036 node-born
secret + TOFU claim + standby adopt) — that firmware half is not yet bench-proven, so prove it on a USB bench
node first: a plug that mints a secret the dictator never learns can only be recovered by opening it.

## Rejected alternatives

- **ESPHome** (Athom's own config, the Levoit pattern): fast, but unsigned control, no identity gate, a third
  firmware family; the asked-for outcome was bespoke firmware.
- **Stay on Tasmota** behind `ha-tasmota-bridge`: same objections; it's what the S31s are being moved off.
- **Serial-flash every plug**: opening 8 mains devices is the slow, risky path; kept as recovery only.
- **Copy the image to 0x10000 and use a stock IDF layout**: our app (~1.06 MB) overlaps itself on the way down
  (source 0xE0000), so the copy would overwrite the code doing the copying.

## Consequences

- A migrated unit keeps **Tasmota's 2nd-stage bootloader**; it may not honour app rollback, so a broken OTA on
  such a unit could need the serial pads. Mitigation: canary every plug OTA on one unit first.
- `ota_0` = `ota_1` = 1600 K; image today 1.06 MB. 832 K `spare` (ex-safeboot) is free for later.
- Server side (adopt as power meter + switchable actuator, driver) is the next step, not part of this ADR's
  first increment.
