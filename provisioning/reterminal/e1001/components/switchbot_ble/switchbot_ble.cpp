#include "switchbot_ble.h"
#include <cctype>
#include <cstring>
#include "esphome/core/log.h"
#include "esphome/components/mqtt/mqtt_client.h"

extern "C" {
#include "switchbot_decode.h"   // lifted pure C decoder — no extern-C guards of its own, so wrap the include
}

namespace esphome {
namespace switchbot_ble {

static const char *const TAG = "switchbot_ble";

bool SwitchbotBle::parse_device(const esp32_ble_tracker::ESPBTDevice &device) {
  if (!this->enabled_)
    return false;   // wall-power gate: don't relay off the battery

  // Pull the SwitchBot service-data (0xFD3D) and manufacturer-data (0x0969) blobs; the decoder wants the bytes
  // AFTER the UUID / company-id, which is exactly what ESPHome hands back in .data.
  const uint8_t *svc = nullptr;
  int svc_len = 0;
  const uint8_t *mfr = nullptr;
  int mfr_len = 0;

  auto svc_uuid = esp32_ble_tracker::ESPBTUUID::from_uint16(SB_SVC_UUID16);
  for (auto &sd : device.get_service_datas()) {
    if (sd.uuid == svc_uuid) {
      svc = sd.data.data();
      svc_len = (int) sd.data.size();
    }
  }
  auto mfr_uuid = esp32_ble_tracker::ESPBTUUID::from_uint16(SB_MFR_COMPANY_ID);
  for (auto &md : device.get_manufacturer_datas()) {
    if (md.uuid == mfr_uuid) {
      mfr = md.data.data();
      mfr_len = (int) md.data.size();
    }
  }

  if (!sb_is_switchbot(mfr != nullptr, svc != nullptr))
    return false;
  sb_reading_t r;
  if (!sb_decode(svc, svc_len, mfr, mfr_len, &r) || !r.valid)
    return false;

  if (mqtt::global_mqtt_client == nullptr || !mqtt::global_mqtt_client->is_connected())
    return true;   // decoded fine, but no bus to relay onto right now

  std::string mac = device.address_str();   // "AA:BB:CC:DD:EE:FF"
  std::string flat;
  for (char c : mac)
    if (c != ':')
      flat += (char) tolower((unsigned char) c);

  char metrics[96];
  if (r.battery_pct >= 0)
    snprintf(metrics, sizeof(metrics), "{\"temperature_c\":%.1f,\"humidity_pct\":%d,\"battery_pct\":%d}",
             r.temperature_c, r.humidity_pct, r.battery_pct);
  else
    snprintf(metrics, sizeof(metrics), "{\"temperature_c\":%.1f,\"humidity_pct\":%d}", r.temperature_c,
             r.humidity_pct);

  // Payload mirrors edge/esp32c6 ha_mqtt.c ha_mqtt_publish_reading (schema 1, transport ble-adv). ts empty:
  // the panel has no wall clock (no SNTP), coordinator stamps arrival — same convention as the edge report.
  char payload[320];
  int n = snprintf(payload, sizeof(payload),
                   "{\"schema\":1,\"node\":\"%s\",\"mac\":\"%s\",\"device_type\":\"%s\",\"ts\":\"\","
                   "\"transport\":\"ble-adv\",\"metrics\":%s,\"meta\":{\"rssi\":%d}}",
                   this->node_.c_str(), mac.c_str(), r.device_type, metrics, device.get_rssi());
  if (n <= 0 || n >= (int) sizeof(payload))
    return true;

  std::string topic = "home/edge/" + this->node_ + "/" + flat + "/adv";
  mqtt::global_mqtt_client->publish(topic, payload, (size_t) n, 1, false);
  this->published_++;
  ESP_LOGD(TAG, "%s %s -> %.1fC %d%% (rssi %d)", r.device_type, mac.c_str(), r.temperature_c, r.humidity_pct,
           device.get_rssi());
  return true;
}

void SwitchbotBle::dump_config() {
  ESP_LOGCONFIG(TAG, "SwitchBot BLE relay: node=%s enabled=%s published=%u", this->node_.c_str(),
                YESNO(this->enabled_), this->published_);
}

}  // namespace switchbot_ble
}  // namespace esphome
