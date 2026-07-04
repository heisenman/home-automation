#include "sy6974b.h"
#include "esphome/core/log.h"

namespace esphome {
namespace sy6974b {

static const char *const TAG = "sy6974b";

bool SY6974B::update_bits_(uint8_t reg, uint8_t mask, uint8_t val) {
  uint8_t cur;
  if (!this->read_reg_(reg, &cur))
    return false;
  cur = (cur & ~mask) | (val & mask);
  return this->write_reg_(reg, cur);
}

void SY6974B::setup() {
  ESP_LOGCONFIG(TAG, "Setting up SY6974B...");
  uint8_t v;
  if (!this->read_reg_(SY6974B_REG08, &v)) {
    ESP_LOGE(TAG, "SY6974B not responding at 0x%02X", this->address_);
    this->mark_failed();
    return;
  }
  // Disable the I2C watchdog (REG05[5:4]=00). Default is 40s: if left on, the charger reverts writable regs
  // (EN_HIZ, CHG_CONFIG, ICHG) to POR defaults whenever I2C goes quiet -> it would silently abort a profiling
  // run mid-cycle. This is the D1001 watchdog lesson applied at the source. We do NOT touch HIZ/charge here:
  // POR leaves charging enabled + HIZ off, so a normal boot still charges.
  if (this->disable_watchdog_) {
    if (this->update_bits_(SY6974B_REG05, 0x30, 0x00))
      ESP_LOGCONFIG(TAG, "I2C watchdog disabled (REG05[5:4]=00) so profiling states hold");
    else
      ESP_LOGW(TAG, "failed to disable I2C watchdog");
  }
}

void SY6974B::update() {
  uint8_t r8, r9 = 0;
  if (!this->read_reg_(SY6974B_REG08, &r8)) {
    this->status_set_warning();
    return;
  }
  this->read_reg_(SY6974B_REG09, &r9);
  this->status_clear_warning();

  this->bus_status_ = (r8 >> 5) & 0x07;
  this->charge_status_ = (ChargeStatus) ((r8 >> 3) & 0x03);
  this->power_good_ = (r8 >> 2) & 0x01;
  this->fault_ = r9;

  uint8_t r0, r1;
  if (this->read_reg_(SY6974B_REG00, &r0))
    this->hiz_ = (r0 >> 7) & 0x01;
  if (this->read_reg_(SY6974B_REG01, &r1))
    this->charge_enabled_ = (r1 >> 4) & 0x01;

#ifdef USE_TEXT_SENSOR
  if (this->charge_status_sensor_ != nullptr) {
    const char *s;
    switch (this->charge_status_) {
      case CHG_NOT_CHARGING: s = "not_charging"; break;
      case CHG_PRE_CHARGE: s = "pre_charge"; break;
      case CHG_FAST_CHARGE: s = "fast_charge"; break;
      case CHG_DONE: s = "done"; break;
      default: s = "unknown"; break;
    }
    this->charge_status_sensor_->publish_state(s);
  }
  if (this->bus_status_sensor_ != nullptr) {
    const char *s;
    switch (this->bus_status_) {
      case 0: s = "no_input"; break;
      case 1: s = "usb_sdp"; break;
      case 2: s = "usb_cdp"; break;
      case 3: s = "usb_dcp"; break;
      case 5: s = "unknown_adapter"; break;
      case 6: s = "nonstd_adapter"; break;
      case 7: s = "otg"; break;
      default: s = "reserved"; break;
    }
    this->bus_status_sensor_->publish_state(s);
  }
#endif
#ifdef USE_BINARY_SENSOR
  if (this->power_good_sensor_ != nullptr)
    this->power_good_sensor_->publish_state(this->power_good_);
  if (this->charging_sensor_ != nullptr)
    this->charging_sensor_->publish_state(this->charge_status_ == CHG_FAST_CHARGE ||
                                          this->charge_status_ == CHG_PRE_CHARGE);
#endif
}

bool SY6974B::set_hiz(bool en) {
  bool ok = this->update_bits_(SY6974B_REG00, 0x80, en ? 0x80 : 0x00);
  if (ok)
    this->hiz_ = en;
  ESP_LOGW(TAG, "set HIZ %s -> %s", ONOFF(en), ok ? "ok" : "FAIL");
  return ok;
}

bool SY6974B::set_charge_enable(bool en) {
  bool ok = this->update_bits_(SY6974B_REG01, 0x10, en ? 0x10 : 0x00);
  if (ok)
    this->charge_enabled_ = en;
  ESP_LOGW(TAG, "set CHG_CONFIG %s -> %s", ONOFF(en), ok ? "ok" : "FAIL");
  return ok;
}

bool SY6974B::set_ichg_ma(uint16_t ma) {
  uint8_t code = ma / 60;          // ICHG = code * 60mA
  if (code > 0x32)                 // 110010 = 3000mA is the register max
    code = 0x32;
  bool ok = this->update_bits_(SY6974B_REG02, 0x3F, code);
  ESP_LOGI(TAG, "set ICHG %umA (code 0x%02X; hw ILIM resistor still caps ~500mA) -> %s", ma, code,
           ok ? "ok" : "FAIL");
  return ok;
}

bool SY6974B::kick_watchdog() { return this->update_bits_(SY6974B_REG01, 0x40, 0x40); }

bool SY6974B::set_watchdog_seconds(uint8_t s) {
  uint8_t code = s == 0 ? 0 : (s <= 40 ? 1 : (s <= 80 ? 2 : 3));  // REG05[5:4]
  return this->update_bits_(SY6974B_REG05, 0x30, code << 4);
}

void SY6974B::dump_config() {
  ESP_LOGCONFIG(TAG, "SY6974B charger:");
  LOG_I2C_DEVICE(this);
  ESP_LOGCONFIG(TAG, "  I2C watchdog disabled: %s", YESNO(this->disable_watchdog_));
  ESP_LOGCONFIG(TAG, "  charging=%s hiz=%s power_good=%s", YESNO(this->charge_enabled_), YESNO(this->hiz_),
                YESNO(this->power_good_));
}

}  // namespace sy6974b
}  // namespace esphome
