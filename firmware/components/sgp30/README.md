# sgp30 — Sensirion SGP30 eCO₂ + TVOC driver (shared component, ADR-0020)

Minimal ESP-IDF (new `i2c_master`) driver. **Pure I²C transport — no secrets, no node policy** — so any
board with an I²C bus can reuse it (edge C3/C6/S3, panel). The board injects the bus, pins, and SCL speed.

## SGP30 vs SGP40 (don't mix them up)
| | SGP30 (this) | SGP40 (`../sgp40`) |
|---|---|---|
| I²C addr | **0x58** | 0x59 |
| Output | **eCO₂ (ppm) + TVOC (ppb)**, on-chip | raw SRAW → VOC Index via `sensirion_gas_index` |
| Companion component | none needed | `sensirion_gas_index` |
| Warmup | ~15 s fixed 400/0, baseline ~12 h | ~45 s |

A bus-scan ACK at **0x58** ⇒ SGP30 (this driver); at **0x59** ⇒ SGP40. See `docs/edge-pinouts.md`.

## Usage
```c
sgp30_t s;
ESP_ERROR_CHECK(sgp30_self_test(&s_predummy));   // optional: run BEFORE init (perturbs the algorithm)
ESP_ERROR_CHECK(sgp30_init(&s, bus, 400000));    // adds device + Init_air_quality (starts 1 Hz algorithm)
// then, at ~1 Hz:
uint16_t eco2, tvoc;
if (sgp30_measure(&s, &eco2, &tvoc) == ESP_OK) { /* publish */ }
```
`sgp30_measure()` **must** be called at ~1 Hz for the dynamic baseline to track. First ~15 reads are the
fixed 400 ppm / 0 ppb warmup. Persist the baseline across reboots with `sgp30_get_baseline()` /
`sgp30_set_baseline()` to skip the ~12 h re-learn (restore only a value stored within the last week).

Protocol: 16-bit big-endian commands, each 16-bit data word followed by CRC-8 (poly 0x31, init 0xFF).
