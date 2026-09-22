# RESUME 2026-09-22 — `hvac_c6` + `dehum_c6` built, flashed, verified

Session end state. Tree clean, `origin/main` current. Both nodes are enrolled, flashed and were
**verified live on the air-gap** before shutdown.

> ⚠️ Expect `home/edge/hvac_c6/status offline` overnight — Hugh took it down deliberately for soldering.
> That retained LWT is not a fault. Same for `dehum_c6` if it was unplugged.

## What exists now

| Node | Build | MAC | fw | State |
|---|---|---|---|---|
| `hvac_c6` | `edge/esp32c6-hvac` | `10:BD:A3:A0:04:E8` | `v1-hvac-listen` | LISTEN_ONLY ERV sniffer, verified through a real power cycle |
| `dehum_c6` | `edge/esp32c6-dehum` | `A0:F2:62:85:6E:60` | `v1-dehum-idle` | inert — relay held de-energized, no control logic |

Both `area: staging`, broker `mqtt://192.168.1.200:1883`, ota_host `192.168.1.210` (ha-2's air-gap leg —
the dev box is `.1.245` on that network). Manifests are board-local: `edge/esp32c6-*/nodes.yaml`.

`hvac_c6` verification, the one that mattered: booted from a **true power cycle** on a power-only USB
port with no serial host, joined `autohome_airgap`, SNTP-synced, reconnected to the broker, and resumed
its 15 s sniff cadence. `writes_refused=0`.

## Next session — the actual install

**Before landing anything on the Broan J9 block** (design doc §7.2):

1. Meter J9 `12V`→`GND`. Confirm 12 VDC and that you are **not on J13** — that block carries 24 VAC for
   the furnace interlock and crossing it into J9 permanently damages the control board.
2. Confirm XIAO GND and Broan GND are **not bonded**. The converter is galvanically isolated; a stray
   bond defeats it silently and everything still appears to work.
3. Terminator check is already done (`A+`↔`B-` = 10 kΩ, jumper open). Re-run only on a replacement board.

Then wire per `edge/esp32c6-hvac/README.md` and read the sniff verdict:

| Symptom | Meaning |
|---|---|
| `rx=0 B` | Not hearing the bus. Move the transceiver's data-out to the other TTL pad — Waveshare's `RXD`/`TXD` labels are ambiguous and crossover is only the *likely* reading. |
| bytes, `frames=0` | **A/B swapped.** `D+`→`B-`, `D-`→`A+`. Non-destructive. |
| high `bad` rate | Marginal bus — a third terminator, or a noisy ground reference. |
| `frames>0`, low `bad` | Working. Telemetry starts appearing on `erv/adv`. |

**Aprilaire is gated** (§7.3 step 1) before its relay contact goes anywhere near `DH`: meter `DH`–`DH`
powered with the relay disconnected, AC *and* DC, and to chassis. Above ~30 V means stop and reassess.

## Open items

- ~~**Node split, undecided.**~~ **RESOLVED 2026-09-22** — Hugh's call: three dedicated nodes, each at
  the thing it controls. ADR-0041 §2 revised; the builds were already separate so nothing moved in code.
- **Server-side, untouched:** `abilities="erv"` and device_type `erv` are new. Intake may read the node
  as relay-only, and metrics will not chart until `METRIC_CATALOG` knows them — a catalog edit needs
  **both** `ha-api` and `ha-api-tls` restarted.
- **`erv/adv` brace fix is host-proven only.** The payload is correctly suppressed until real registers
  arrive, so the wire format has never been observed. First real frames will confirm it.
- **Temps publish as `*_temp_raw`** because °C-vs-°F is unresolved (open question #6). Renaming after
  §7.2 settles it is a deliberate, accepted one-time cost.
- **`ha_aprilaire_dehum` does not exist** and should not be written before §7.3 characterization, or the
  controller will fight the unit's own anti-short-cycle and defrost protections.
- **E50 recovery unknown** (§1.8) — still blocking any move off LISTEN_ONLY.

## Techniques worth reusing

**Catching a board that deep-sleeps.** `esptool --before default_reset` reboots straight into the ROM
download mode so the sleeping app never runs; `--after no_reset` parks it there because the ROM does not
sleep. A 50 ms polling watcher wins the window — no BOOT button, no tiny pad to short.
Script: `pounce.sh` pattern in this session's scratchpad.

**`erase_flash` alone does not give a stable port.** It wipes the second-stage bootloader too, so the ROM
finds no image and resets every ~2.5 s, re-enumerating each cycle. Expected for a blank chip, not a fault.

**Read `app_desc` before overwriting an unknown board.** `read_flash 0x10000 0x100`, magic `0xABCD5432`
at offset `0x20`, then `version[32]` at `0x30` and `project_name[32]` at `0x50`. Told us the dehum board
held only a stock `arduino-lib-builder` image, so overwriting cost nothing.

**Do not pipe `mosquitto_sub` through `grep | head` under `timeout`.** grep block-buffers on a pipe and
the SIGTERM kills it before the flush — it reads as "the node is publishing nothing." Redirect to a file
and grep the file. This produced a false negative twice in this session.
