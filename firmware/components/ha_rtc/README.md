# ha_rtc — PCF8563 real-time clock driver

Ability **G** (wall clock) from [d1001-capability-roadmap.md](../../../docs/design/d1001-capability-roadmap.md).
A hardware wall clock that survives reboots (holdover while the device rail is powered) plus a
**valid-time gate** so a renderer never shows a bogus time.

**Contract:** [ADR-0019](../../../docs/adr/ADR-0019-screen-interface-architecture.md) (panel); time-authority
discipline per [ADR-0010](../../../docs/adr/ADR-0010-command-control-protocol.md) — the server is authoritative,
the RTC is a local holdover the SNTP hook sets. Grounded in `docs/hardware/PCF8563.pdf`.

## Platform support
- **D1001** (PCF8563T, U20, I2C1 @ `0x51`): inject `bsp_i2c1()` as the bus.
- Any board with a PCF8563/PCF8564-class RTC on an `i2c_master` bus — pass a different `addr` if it differs.

## Shape (module-first)
| File | Role | IDF? |
|---|---|---|
| `ha_rtc_regs.c` | **pure** BCD↔`struct tm`, VL + century decode | no — host-tested |
| `ha_rtc.c` | I2C transport (read/set/probe) over the injected bus | yes |
| `include/ha_rtc.h` | contract; `ha_rtc_cfg_t {bus, addr}` injected by the BSP | — |
| `test/` | host test (`run.sh`, plain `cc`) for the pure core | no |

## Use
```c
ha_rtc_init(&(ha_rtc_cfg_t){ .bus = bsp_i2c1(), .addr = HA_RTC_PCF8563_ADDR });
struct tm now; bool valid;
if (ha_rtc_get(&now, &valid) == ESP_OK && valid) { /* render the clock */ }
// on SNTP sync: ha_rtc_set(&synced_tm);  // clears VL -> becomes the reboot-holdover source
```

`ha_rtc_get` returns `valid = false` when the RTC's **VL** flag is set (oscillator integrity lost since the
last set) — the renderer must not display a clock until an `ha_rtc_set` (SNTP) clears it. Transactions are
capped at 100 ms per the datasheet's interface-watchdog note (an access held >1 s loses a second).

## Test
```
./test/run.sh    # pure register core: BCD, VL, century, round-trip, epoch-via-timegm
```
