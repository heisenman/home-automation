// sy6974b — ESPHome external component: I2C driver for the Silergy SY6974B power-path charger (addr 0x6B).
// The reusable "IC library" module: register map + watchdog handling + status decode + a gated control API
// (HIZ input-disconnect, charge enable, charge-current). This is what makes automated battery profiling on the
// E1001 possible where the D1001 could not — its BQ was off-I2C (ADR-0024), so forced discharge was human-only.
// Register map: docs/hardware/DS_SY6974B.pdf. Wiring: reTerminal_E1001_V1_2_SCH sheet 4 (I2C1, /CE tied, ILIM~500mA).
//
// SAFETY: HIZ ON runs the system on battery with USB still plugged (the discharge lever) — a low cell can
// brown out. Charge-disable + watchdog-off means a hung host leaves those states latched. The safety state
// machine (V floor/ceiling, thermal gate, run lifecycle) lives in the profiling CONTROLLER (module 2), not here;
// this module only exposes the mechanism.
#pragma once
#include "esphome/core/component.h"
#include "esphome/components/i2c/i2c.h"
#ifdef USE_TEXT_SENSOR
#include "esphome/components/text_sensor/text_sensor.h"
#endif
#ifdef USE_BINARY_SENSOR
#include "esphome/components/binary_sensor/binary_sensor.h"
#endif

namespace esphome {
namespace sy6974b {

// Registers (see datasheet §I2C Registers, address 0x6B)
static const uint8_t SY6974B_REG00 = 0x00;  // EN_HIZ[7] STAT_DIS[6:5] IINLIM[4:0]
static const uint8_t SY6974B_REG01 = 0x01;  // PFM_DIS[7] WD_RST[6] OTG_CONFIG[5] CHG_CONFIG[4] SYS_MIN[3:1]
static const uint8_t SY6974B_REG02 = 0x02;  // BOOST_LIM[7] Q1_FULLON[6] ICHG[5:0] (code*60mA)
static const uint8_t SY6974B_REG05 = 0x05;  // EN_TERM[7] WATCHDOG[5:4] EN_TIMER[3] CHG_TIMER[2] TREG[1]
static const uint8_t SY6974B_REG08 = 0x08;  // RO: BUS_STAT[7:5] CHRG_STAT[4:3] PG_STAT[2] THERM_STAT[1] VSYS_STAT[0]
static const uint8_t SY6974B_REG09 = 0x09;  // RO: fault bits (WATCHDOG_FAULT[7] BOOST/CHRG/BAT/NTC ...)

// REG08[4:3]
enum ChargeStatus : uint8_t {
  CHG_NOT_CHARGING = 0,
  CHG_PRE_CHARGE = 1,
  CHG_FAST_CHARGE = 2,
  CHG_DONE = 3,
};

class SY6974B : public PollingComponent, public i2c::I2CDevice {
 public:
  void setup() override;
  void update() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::DATA; }

  // config-time wiring
  void set_disable_watchdog(bool b) { this->disable_watchdog_ = b; }
#ifdef USE_TEXT_SENSOR
  void set_charge_status_sensor(text_sensor::TextSensor *s) { this->charge_status_sensor_ = s; }
  void set_bus_status_sensor(text_sensor::TextSensor *s) { this->bus_status_sensor_ = s; }
#endif
#ifdef USE_BINARY_SENSOR
  void set_power_good_sensor(binary_sensor::BinarySensor *s) { this->power_good_sensor_ = s; }
  void set_charging_sensor(binary_sensor::BinarySensor *s) { this->charging_sensor_ = s; }
#endif

  // control API (called by the profiling controller / bench template switches)
  bool set_hiz(bool en);                  // REG00[7]  — input disconnect: run on battery with USB in
  bool set_charge_enable(bool en);        // REG01[4]  — gate charging (/CE pin is tied, so this is the control)
  bool set_ichg_ma(uint16_t ma);          // REG02[5:0]— fast-charge current; NOTE hw ILIM resistor caps ~500mA
  bool kick_watchdog();                   // REG01[6]=1
  bool set_watchdog_seconds(uint8_t s);   // REG05[5:4]: 0=disable, else 40/80/160

  // cached reads (refreshed each update())
  ChargeStatus charge_status() const { return this->charge_status_; }
  bool power_good() const { return this->power_good_; }
  bool hiz() const { return this->hiz_; }
  bool charge_enabled() const { return this->charge_enabled_; }
  uint8_t bus_status() const { return this->bus_status_; }
  uint8_t fault() const { return this->fault_; }

 protected:
  bool read_reg_(uint8_t reg, uint8_t *val) { return this->read_byte(reg, val); }
  bool write_reg_(uint8_t reg, uint8_t val) { return this->write_byte(reg, val); }
  bool update_bits_(uint8_t reg, uint8_t mask, uint8_t val);

  bool disable_watchdog_{true};
  ChargeStatus charge_status_{CHG_NOT_CHARGING};
  bool power_good_{false};
  bool hiz_{false};
  bool charge_enabled_{true};
  uint8_t bus_status_{0};
  uint8_t fault_{0};
#ifdef USE_TEXT_SENSOR
  text_sensor::TextSensor *charge_status_sensor_{nullptr};
  text_sensor::TextSensor *bus_status_sensor_{nullptr};
#endif
#ifdef USE_BINARY_SENSOR
  binary_sensor::BinarySensor *power_good_sensor_{nullptr};
  binary_sensor::BinarySensor *charging_sensor_{nullptr};
#endif
};

}  // namespace sy6974b
}  // namespace esphome
