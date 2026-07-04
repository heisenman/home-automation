# sy6974b — ESPHome external component (hub) for the Silergy SY6974B I2C power-path charger.
# Exposes decoded status as optional text/binary sensors; the control API (HIZ / charge-enable / ICHG) is C++
# public methods driven from YAML template switches (bench) or the profiling controller (module 2).
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import i2c, text_sensor, binary_sensor
from esphome.const import CONF_ID

DEPENDENCIES = ["i2c"]
AUTO_LOAD = ["text_sensor", "binary_sensor"]
MULTI_CONF = True

sy6974b_ns = cg.esphome_ns.namespace("sy6974b")
SY6974B = sy6974b_ns.class_("SY6974B", cg.PollingComponent, i2c.I2CDevice)

CONF_DISABLE_WATCHDOG = "disable_watchdog"
CONF_CHARGE_STATUS = "charge_status"
CONF_BUS_STATUS = "bus_status"
CONF_POWER_GOOD = "power_good"
CONF_CHARGING = "charging"

CONFIG_SCHEMA = (
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(SY6974B),
            # default False: leave the I2C watchdog at its 40s HW default. Holding HIZ/charge states during a
            # profiling run is the PROFILER's job (it kicks WD_RST) — keeping the watchdog ON means a profiler
            # crash auto-recovers (WD expiry resets EN_HIZ->0, input reconnects). See battery_profiler safety loop.
            cv.Optional(CONF_DISABLE_WATCHDOG, default=False): cv.boolean,
            cv.Optional(CONF_CHARGE_STATUS): text_sensor.text_sensor_schema(),
            cv.Optional(CONF_BUS_STATUS): text_sensor.text_sensor_schema(),
            cv.Optional(CONF_POWER_GOOD): binary_sensor.binary_sensor_schema(),
            cv.Optional(CONF_CHARGING): binary_sensor.binary_sensor_schema(),
        }
    )
    .extend(cv.polling_component_schema("60s"))
    .extend(i2c.i2c_device_schema(0x6B))
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await i2c.register_i2c_device(var, config)
    cg.add(var.set_disable_watchdog(config[CONF_DISABLE_WATCHDOG]))

    if CONF_CHARGE_STATUS in config:
        cg.add(var.set_charge_status_sensor(await text_sensor.new_text_sensor(config[CONF_CHARGE_STATUS])))
    if CONF_BUS_STATUS in config:
        cg.add(var.set_bus_status_sensor(await text_sensor.new_text_sensor(config[CONF_BUS_STATUS])))
    if CONF_POWER_GOOD in config:
        cg.add(var.set_power_good_sensor(await binary_sensor.new_binary_sensor(config[CONF_POWER_GOOD])))
    if CONF_CHARGING in config:
        cg.add(var.set_charging_sensor(await binary_sensor.new_binary_sensor(config[CONF_CHARGING])))
