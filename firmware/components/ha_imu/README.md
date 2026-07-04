# ha_imu — LSM6DS3TR-C IMU driver

The on-board 6-axis IMU (D1001 U20, I2C1 @ `0x6A`, `6D_INTn` → P4): board **temperature**, raw **accel**,
and the hardware **wake/tap engine** (presence + tap-to-wake). Ability **A**/roadmap #3
([d1001-capability-roadmap.md](../../../docs/design/d1001-capability-roadmap.md)); grounded in
`docs/hardware/lsm6ds3tr-c.pdf`.

## Shape (module-first)
| File | Role | IDF? |
|---|---|---|
| `ha_imu_regs.c` | **pure** unit conversions (temp/accel/threshold) | no — host-tested |
| `ha_imu.c` | I2C transport + wake/tap engine setup | yes |
| `include/ha_imu.h` | contract; `ha_imu_cfg_t {bus, addr}` injected by the BSP | — |
| `test/` | host test (`run.sh`) for the pure core | no |

## Composition — how two real components synthesize on one chip

The LSM6DS3TR-C is a **single physical device that two features need**: `ha_battery` reads its temperature
for thermal-gated charging (ADR-0024), and the panel presence/tap-to-wake feature drives its wake engine
(roadmap #3). The wrong way to "reuse" is for each to open its own I2C handle — they would then both write
`CTRL1_XL` (battery wants a low ODR for temp; presence wants a higher ODR + the wake filter) and **stomp each
other's config on the shared silicon**. Two logical handles ≠ two independent devices.

So `ha_imu` **owns the chip**, and the other component **composes on top of it** rather than duplicating it:

```
        ha_imu  ──────────────┐  owns the one device handle + the one config (CTRL1_XL, CTRL3_C)
        (component)           │  ha_imu_init() is IDEMPOTENT — first caller configures, rest are no-ops
          ▲            ▲      │
          │ temp_dc()  │ events_enable()/poll()
          │            │
     ha_battery     panel presence task      ← two independent consumers, one shared, correctly-configured chip
     (component)    (device glue)
```

The seam is the **injected `ha_imu_cfg_t {bus, addr}`** (the board BSP owns the pins — `bsp_i2c1()` / `0x6A`)
plus the **idempotent `ha_imu_init()`**. Every consumer calls `ha_imu_init()` with the same cfg; whoever runs
first sets the config, the rest return `ESP_OK` immediately. `ha_battery` therefore no longer contains any IMU
register code — it just calls `ha_imu_init()` + `ha_imu_temp_dc()`. This is the ADR-0020 pattern: a capability
that more than one build/feature needs becomes **one shared component with an injected board seam**, consumed by
thin glue — not copied, and not fought over.

*(This is a worked example of two components composing; the general method is in
[docs/DEVICE-INTAKE.md](../../../docs/DEVICE-INTAKE.md) Stage 3–4.)*

## Use
```c
ha_imu_init(&(ha_imu_cfg_t){ .bus = bsp_i2c1(), .addr = HA_IMU_LSM6_ADDR });  // idempotent
int dc; ha_imu_temp_dc(&dc);                       // ha_battery: board temp, deci-°C
ha_imu_events_enable(125);                          // presence: wake @ ~125 mg + single-tap -> INT1
bool motion, tap; ha_imu_poll(&motion, &tap);       // presence task: read WU_IA / single-tap (clears latch)
```

## Test
```
./test/run.sh    # pure conversions: temp, accel-mg, wake-threshold clamp
```
