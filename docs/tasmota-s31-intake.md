# SONOFF S31 intake (Tasmota → our bus)

Turnkey runbook for flashing a SONOFF S31 to Tasmota and landing its energy telemetry on our MQTT bus.
First job: **wall-power meter for the G11 (210)** feeding the power-optimization campaign. Then it can be
repositioned onto any air purifier as a controllable + metering plug.

The S31 ships as **eWeLink cloud** — useless to us until reflashed. After flashing it is **fully local**
(no cloud, talks only to our broker). Same CP2102 UART adapter is reused for the Levoit V201S reflash.

## 0. Download once (needs internet — do it before going to the bench)
- Tasmota binary `tasmota.bin` (standard build includes the CSE7766 energy driver + S31 template):
  `http://ota.tasmota.com/tasmota/release/tasmota.bin`
- `esptool` (host): `pipx install esptool` (or `pip install esptool`).

## 1. Flash (mains UNPLUGGED the whole time)
**Safety:** the S31 is a mains device. Never connect the UART adapter while it's plugged into the wall.
Flash on the bench, de-energized, powered only from the adapter's **3.3 V** (5 V will brick it).

Wiring — S31 internal pads (use the **RX/TX pair nearest the VCC pad**):
```
adapter 3V3  -> S31 VCC
adapter GND  -> S31 GND
adapter TX   -> S31 RX
adapter RX   -> S31 TX
```
Bootloader: **hold the S31 button (GPIO0) while connecting VCC**, then release.

Flash:
```
esptool --port /dev/ttyUSB0 erase_flash
esptool --port /dev/ttyUSB0 write_flash 0x0 tasmota.bin
```
Power-cycle (re-enter bootloader hold not needed for normal boot). The S31 raises a `tasmota-XXXX` WiFi AP.

## 2. Join the LAN + configure (Tasmota web UI)
Join the AP, browse to `192.168.4.1`, set WiFi to the house SSID. Once it's on the LAN, open its IP →
**Consoles → Console** and paste:

```
Backlog Module 41; Topic plug_g11; FullTopic %prefix%/%topic%/
Backlog MqttHost 192.168.0.200; MqttPort 1883; MqttUser ; MqttPassword ; TelePeriod 30
```
- `Module 41` = SONOFF S31 (relay + CSE7766 metering).
- `Topic plug_g11` = the key the bridge maps (see step 4). Use a per-deployment name (e.g.
  `plug_purifier_office` when it moves).
- Broker is anonymous-on-LAN, so MqttUser/Password are blank.
- `TelePeriod 30` → energy telemetry every 30 s on `tele/plug_g11/SENSOR`.

It now publishes:
- `tele/plug_g11/SENSOR` → `{"ENERGY":{"Power":..,"Voltage":..,"Current":..,"Total":..,"Today":..,"Factor":..}}`
- `tele/plug_g11/STATE`  → relay `POWER:ON|OFF` + `Wifi.Signal` (dBm)

## 3. Energy calibration (CSE7766 — optional but improves accuracy)
With a **known resistive load** (e.g. a labelled incandescent bulb / heater) plugged in and ON, in the
console:
```
PowerSet <known_watts>
VoltageSet <measured_mains_volts>
CurrentSet <measured_milliamps>
```
Voltage alone is usually close from the factory; set Power against a known load for the campaign.

## 4. Land it on our bus (system side — ops)
1. Add the device to the Tasmota registry on the dictator (210):
   `instance/tasmota-devices.yaml` (copy from `.example`):
   ```yaml
   plug_g11:
     device_id: plug_g11
     area: infra
     device_type: energy_meter
   ```
2. Install + start the bridge (new unit → Hugh runs the install; restart-only thereafter):
   ```
   sudo cp systemd/ha-tasmota-bridge.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now ha-tasmota-bridge
   ```
   `server/ingest/tasmota_bridge.py` subscribes to `tele/+/SENSOR|STATE`, maps the ENERGY/STATE blocks to
   canonical metrics (`power_w, voltage_v, current_a, energy_kwh, relay_on, …`), and republishes
   `home/<area>/<device_id>/state` — which the existing writer ingests into `hot.db` (no writer/dashboard
   change; same UNIQUE(device_id,ts,metric) idempotency as every other reading).
3. Verify: `mosquitto_sub -h 192.168.0.200 -t 'home/infra/plug_g11/state' -v` shows mapped metrics, and the
   readings land in `hot.db` / the PWA.

## 5. Repositioning onto a purifier (later)
- Re-point the Tasmota topic (`Backlog Topic plug_purifier_office`) and add the matching registry entry.
- For on/off **control**, the reverse path is `cmnd/<topic>/POWER ON|OFF`; wire that into the control layer
  when the plug graduates from meter to actuator (a follow-up — `tasmota_bridge` is read-only today).
