// switchbot_ble — ESPHome wrapper around the LIFTED pure decoder switchbot_decode.c (the edge firmware's shared
// component, copied verbatim). Listens to esp32_ble_tracker adverts, decodes SwitchBot meters, and publishes to
// the edge contract home/edge/<node>/<macflat>/adv (payload mirrors edge/esp32c6 ha_mqtt.c:270 exactly). Gated on
// wall power (set_enabled from the sy6974b power_good) so it never scans-and-relays off the battery — mirrors the
// D1001's pause-on-battery (beachhead_main.c power_task). Stage 2 of the E1001 edge-node.
#pragma once
#include <string>
#include "esphome/core/component.h"
#include "esphome/components/esp32_ble_tracker/esp32_ble_tracker.h"

namespace esphome {
namespace switchbot_ble {

class SwitchbotBle : public Component, public esp32_ble_tracker::ESPBTDeviceListener {
 public:
  bool parse_device(const esp32_ble_tracker::ESPBTDevice &device) override;
  void dump_config() override;
  void set_node(const std::string &n) { this->node_ = n; }
  void set_enabled(bool en) { this->enabled_ = en; }   // wall-power gate (true on wall, false on battery)

 protected:
  std::string node_{"e1001"};
  bool enabled_{true};
  uint32_t published_{0};
};

}  // namespace switchbot_ble
}  // namespace esphome
