# switchbot_ble — ESPHome external component: esp32_ble_tracker listener that decodes SwitchBot meter adverts
# (via the lifted pure switchbot_decode.c) and relays them to home/edge/<node>/<mac>/adv. Stage 2 of the edge node.
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import esp32_ble_tracker
from esphome.const import CONF_ID

DEPENDENCIES = ["esp32_ble_tracker", "mqtt"]

switchbot_ble_ns = cg.esphome_ns.namespace("switchbot_ble")
SwitchbotBle = switchbot_ble_ns.class_(
    "SwitchbotBle", cg.Component, esp32_ble_tracker.ESPBTDeviceListener
)

CONF_NODE = "node"

CONFIG_SCHEMA = (
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(SwitchbotBle),
            cv.Optional(CONF_NODE, default="e1001"): cv.string,
        }
    )
    .extend(esp32_ble_tracker.ESP_BLE_DEVICE_SCHEMA)
    .extend(cv.COMPONENT_SCHEMA)
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await esp32_ble_tracker.register_ble_device(var, config)
    cg.add(var.set_node(config[CONF_NODE]))
