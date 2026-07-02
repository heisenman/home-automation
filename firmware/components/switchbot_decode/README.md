# switchbot_decode

**Role:** SwitchBot BLE advertisement bytes → `sb_reading_t` (temperature / humidity / battery / device_type).
Pure C, no ESP or Bluetooth dependencies — the exact unit ADR-0003 would later repackage as a WASM module.

**Contract:** `include/switchbot_decode.h`.
- `sb_is_switchbot(has_0969_mfr, has_fd3d_svc)` — cheap prefilter.
- `sb_decode(svc, svc_len, mfr, mfr_len, &out)` — returns true + fills `out` (with `out.valid`) for a
  decodable meter. Handles Format A (full service data: Meter/Plus/older Pro) and Format B (manufacturer data
  behind a 6-byte MAC prefix: Outdoor/newer Pro). Validation ranges: temp −40..60 °C, humidity 0..100 %.

**Canonical parity:** behaviour is a straight port of `server/ingest/decoders/switchbot.py` — edge-scanned and
server-scanned readings **must** agree. Change them together.

**Platform support:** any (pure). No `REQUIRES`.

**Provenance:** extracted verbatim (ADR-0020 Stage 1) from the byte-identical `edge/{esp32c3,esp32c6,esp32s3-eth}/main/switchbot_decode.{c,h}`
forks (sha256 `d484dcf…`). Live edge nodes still link their fork copy until the gated Stage-2 migration.

## Test (no IDF required)

```sh
./test/run.sh        # plain cc; exits non-zero on any failed check
```

Covers Format A, Format B, negative temperature, and out-of-range rejection.
