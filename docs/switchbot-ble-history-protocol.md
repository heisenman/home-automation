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

## ✅ RESOLVED — meter_pro live edge pull works (2026-07-26)

The dead-end below was a **metadata frame-type change on current meter firmware**. Diagnosed by driving the
live meter through the generic GATT forwarder (`op:gatt`/`tools/edge_gatt.py`) and reading the raw frames:

- The active-bank metadata type **flipped `0x69` → `0x6a`** on current Meter Pro firmware. The old parser
  accepted only `0x69`, so it discarded the one frame carrying the write pointer → `newest=0`.
- Current firmware emits **two `0x6a` frames**: the real buffer descriptor (write pointer, e.g. `0x6682`,
  device-clock ts) and a **now-echo** that reflects the handshake time with **ptr=0**. The meter no longer
  reports a distinct *oldest* pointer.

**Fix** (`firmware/components/ha_gatt/ha_gatt.c`, shipped as `v23-mphist`): accept `0x6a`, ignore the
`ptr==0` echo, take the single write pointer as `newest`, and **window back** from it (synthesizing the
oldest anchor) — the same model the outdoor path already used. Added a tunable `op:history {"window":N}`
knob (default 256 records near the pointer; the meter drops the link if paged far below its valid range).
Server ingest (`server/ingest/edge_history.py`) anchors the last relayed record to `newest_ts` and counts
backward at the interval (robust to the address→sample-index slide).

**Proven end-to-end** on `meter_pro_h_bed` via `hbed_c6`: newest recovered sample `24.4°C/40%` matched the
live advertised `24.3°C/40%`, timestamped ~now.

**Frozen-buffer guard** (`edge_history.py`): a dead meter (`meter_pro_c_office` — history clock stopped
~35 days ago, still advertises live) returns the same static write pointer + stale records; reanchoring
them to "now" would inject stale data. The guard refuses a pull whose pointer hasn't advanced since the
last pull, and a large-drift first pull whose newest record disagrees with the live reading. Such a meter
needs a SwitchBot-app time resync before its history is usable.

Recover (`RECOVER_ENABLED`) is now LIVE. `MP_INTERVAL_S` (per-address interval) is a 60 s placeholder —
calibrate from real cadence; it only affects intra-window spacing, never the (reanchored) newest sample.

---

## ⚠️ (historical) DEAD-END — live edge pull returns no anchored samples (2026-07-23)

Status as of 2026-07-23: **history recovery does NOT work end-to-end on the live edge fleet.** A
correctly-signed `op:history` fired at a co-located C6 node (prod: `coffice_c6` → `meter_pro_c_office`,
firmware `v20-battfix`) was observed on the node log (`home/edge/<node>/log`):

```
pull …: connecting (addr_type=0, profile=meter_pro)
connect status=0                 ← GATT connect OK
discovered: cmd=19 notify=16     ← SwitchBot svc found
notif[15]=016a0434096a37405100006ce80078   ← notifications DO stream
meta try 0: count=2 newest=0 oldest=0      ← but metadata reads ZERO records
meta try 1: count=4 newest=0 oldest=0
```

So the node **connects and streams**, but the on-device metadata reassembly yields `newest=0 oldest=0`
→ no records to page → nothing decoded → nothing ingested (the outage gap did NOT fill).

**Ruled out this session:** clock-skew (firmware `history` freshness window is 300s vs 86400s for
`batt_refresh`/`ota`, but the node *accepted and acted on* the command — not a `stale` reject); routing
(co-located node, `connect status=0`); signing (LUT `cmd_secret` verified, node acted). The failure is in
the **metadata/record decode** — the `count>0 newest=0 oldest=0` path — i.e. the handshake/metadata step
above (plan steps 3–4) does not actually surface the ring-buffer indices on this firmware. The prior
"confirmed against captures" was an **offline btsnoop decode**, never a proven live device→hot.db pull.

**Next:** capture a fresh live btsnoop of the metadata exchange on `v20-battfix`, compare against the
offline capture the profiles were derived from, and fix the on-device index read. Until then recover stays
gated (`RECOVER_ENABLED=false`). Tracked: `docs/FOLLOWUPS.md` (2026-07-23) + memory `edge-history-pull-broken`.
