# SwitchBot Meter — BLE History Protocol (reverse-engineered)

**Status:** WIP — command structure + value encoding confirmed; exact record framing being finalized.
**Source:** Android `btsnoop_hci.log` capture of the SwitchBot app pulling a meter's on-device
history (Pixel 6 Pro, 2026-06-20). Raw capture kept in `instance/research/` (gitignored).
**Why this exists:** SwitchBot does NOT document the history command (public `meter.md` has only
`0x31` = current readings) and no library implements it — but the meters store 36–68 days
on-device and stream it over BLE. This is our own RE of that undocumented protocol (ADR-0007).

## GATT

- SwitchBot custom service `cba20d00-224d-11e6-9fb8-0002a5d5c51b`
- **Command (write) characteristic → ATT handle `0x0013`** in this capture. All commands begin
  with the SwitchBot magic byte `0x57`.
- Responses arrive as **ATT Handle-Value Notifications (`0x1b`)** on the notify characteristic
  (`cba20003-…`); the app first enables its CCCD.

## Command sequence (host → device, writes to 0x0013)

1. **Time/handshake:** `5700 05 03 04 00000000 <unixLE/ BE?>` — carries the current time;
   observed value `6a36ec5e` = **big-endian Unix seconds** ≈ 2026-06-20.
2. **Range/config:** `570f68 05 04 01 03 08 02 00 0b 01 02 00 0e 10` (selects what to read).
3. **Enable/streaming setup:** `570f6908 01`, `570f6908 0202`, `570f6908 0201`.
4. **Paginated history read (the bulk):** `570f69 0803 0200 00 <ADDR:2 BE> 06`
   - `ADDR` increments by **6** each request: `0x7842, 0x7848, 0x784e, … 0x7a64`.
   - `ADDR` is an offset into the device's **circular log buffer**; the current write pointer
     is reported in a metadata notification (`0x7a66` here, just past the last read).

## Responses (device → host, notifications 0x1b)

- Each notification is `01` (status/OK) + payload.
- **Metadata** notifications (len 15), e.g. `01 69 fd8c83 6a36ebdb 0000 7a66 0078`:
  - `6a36ebdb` = **big-endian Unix base timestamp** (1,781,407,195 ≈ 2026-06-20)
  - `7a66` = circular-buffer write pointer (matches the read-address space)
- **Data** notifications (len 16) = `01` + 15 bytes of packed samples.
  - Samples use the **same encoding as the advertisement**: temperature byte `t` →
    `(t & 0x7f)` °C, positive when bit 7 set; humidity byte `h` → `(h & 0x7f)` %.
  - Confirmed: `96 2b` = 22°C/43%, `97 2c` = 23°C/44% — matched the live house readings.
  - **TODO:** nail the exact 16-byte record layout — the per-sample stride, the embedded
    index/timestamp bytes (a small marker recurs ~every 5 bytes), the temperature fractional
    nibble (advertisement carries 0.1°C; confirm whether history keeps it or is integer-only),
    and how each sample's timestamp derives from the base time + buffer index + sample interval.

## Implementation plan (`tools/switchbot_history.py`)

1. `bleak` connect to a meter by MAC (from `instance/devices.yaml`).
2. Enable notifications on `cba20003-…`; collect into a buffer.
3. Write the handshake + range + paginated read commands to handle `0x0013`.
4. Reassemble notifications → decode records → `(ts, temperature_c, humidity_pct)`.
5. `INSERT OR IGNORE` into `readings` (the `UNIQUE(device_id,ts,metric)` index makes re-pulls
   safe — pull the full 68-day window every run; only new rows land).
6. Per-model validation (Meter Pro vs Outdoor Meter may differ — capture each).
7. Needs the dedicated BT dongle (connection-based; heavier on the radio than passive scan).

## Per-model command profiles (2026-06-20)

The handshake (`5700…`+unix time), metadata format, and record format `[t,h,frac,t,h]` are
**shared** across models — only the setup + read commands differ. Both confirmed against
captures (Meter Pro = `meter_pro_master_bed`; Outdoor = `meter_living_room`).

| | Meter Pro (`meter_pro`) | Outdoor Meter (`outdoor`) |
|---|---|---|
| setup | `570f68…`, `570f690801`, `570f690802 02/01` | `570f3a`, `570f3b01`, `570f3b00` |
| read  | `570f6908 0302 0000 <addr:2BE> 06` | `570f3c 01 0000 <addr:2BE> 06` |
| interval (metadata-derived) | ~354 s | ~142 s |

`tools/switchbot_history.py` picks the profile from device_type (`*outdoor*` → outdoor).
Same `decode_meter_pro()` + `assign_timestamps()` for both.
