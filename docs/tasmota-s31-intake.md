# Tasmota device intake — finalized procedure (Sonoff S31 + general Tasmota)

Turnkey, battle-tested procedure for flashing a Sonoff S31 (or any ESP8266/8285 Tasmota device) and landing
its telemetry on our MQTT bus. Rewritten 2026-07-06 after the `airgap_router_pm` intake, which exposed several
wrong assumptions in the old runbook. **Read the Division of Labor and Gotchas first — they save an hour.**

The S31 ships as **eWeLink cloud** — useless to us until reflashed. After flashing it is **fully local** (talks
only to our broker). Same CP2102 UART adapter is reused for the Levoit V201S reflash.

---

## 0. Division of labor (THE key lesson)
The device's serial console and its HTTP command API are both **unreliable/unavailable during intake** (see
Gotchas). So the reliable split is:

- **dev (on 210):** flash over the programmer; then, once the device is on the broker, do ALL config **over
  MQTT** (`cmnd/<topic>/…`); verify; register.
- **user (physically local to the device):** the real **power cycle** after flash, and the **WiFi + MQTT-host
  setup via the device's AP web page**. The local user is the reliable actuator for anything network-config,
  because dev's remote HTTP is flaky and the serial console dies mid-config.

Signal discipline: when dev hands the user a step, dev goes **silent** until the user says done (see
memory `signal-turn-taking-with-hugh`).

---

## 1. Prereqs (once)
- **`tasmota.bin`** on 210 at `instance/firmware/tasmota/tasmota.bin` (gitignored vendor blob + `PROVENANCE.md`;
  fetch `http://ota.tasmota.com/tasmota/release/tasmota.bin` on an internet box, `sha256sum`-verify, copy over).
- **esptool** — already on 210 via the IDF env: `. ~/esp/esp-idf/export.sh && python -m esptool version`.

## 2. Flash (dev — mains UNPLUGGED the whole time)
**Safety:** mains device. Never connect the UART adapter while it's plugged into the wall. Power ONLY from the
adapter's **3.3 V** (5 V bricks it — check the adapter's 3V3/5V jumper).

Wiring — S31 internal pads (RX/TX pair nearest VCC). **UART is crossed:**
```
adapter 3V3 -> S31 VCC      adapter TX -> S31 RX
adapter GND -> S31 GND      adapter RX -> S31 TX
```
Bootloader: **hold the S31 button (GPIO0) while VCC connects**, then release. Then (dev, on 210):
```
. ~/esp/esp-idf/export.sh
sha256sum -c <(echo "<sha> instance/firmware/tasmota/tasmota.bin")
python -m esptool --port /dev/ttyUSB0 --chip esp8266 erase_flash
python -m esptool --port /dev/ttyUSB0 --chip esp8266 write_flash 0x0 instance/firmware/tasmota/tasmota.bin
```
S31 flash chip is **DOUT** (esptool keeps the image header's mode; don't override to qio/dio).

## 3. Power cycle (USER) — do NOT skip
After flashing, the chip needs a **real power cycle** (fully remove VCC, reconnect — no button) before the new
firmware runs and the console/network behave. A DTR/RTS reset is NOT enough. (memory `mcu-power-cycle-after-flash`.)

## 4. WiFi + MQTT host (USER, via the device AP page)
Do NOT set WiFi over serial — the console drops when Tasmota restarts to apply it (memory
`tasmota-wifi-intake-is-user-step`). Instead, on a phone/laptop:
1. Join the device's `tasmota-XXXX` WiFi.
2. Browse `192.168.4.1` → set WiFi **`CTWap_24g`** / `<psk>` → Save. Device reboots onto the LAN.
3. Reconnect to the house WiFi, browse to the device's new IP → **Configuration → Configure MQTT → Host =
   `192.168.0.210`** → Save. **Use `.210`, NOT the VIP `.200`** — `.200` is unreachable from CTWap_24g WiFi
   (memory `active-network-main-not-airgap`), and a device pointed at `.200` sits there forever, unreachable.
4. Hand dev the device's **LAN IP** (router client list or the device page). Device is now on the broker under
   its default topic `tasmota_XXXXXX`.

## 5. Final topic + module (dev, over MQTT — reliable)
The device is now on the broker, so configure it over MQTT (NOT `/cm` — HTTP API is disabled by default; NOT
serial — flaky). Find its default topic (`mosquitto_sub -h localhost -t 'tele/+/LWT' -v` shows the new
`tasmota_XXXXXX`), then:
```
mosquitto_pub -h localhost -t 'cmnd/tasmota_XXXXXX/Backlog' -m 'Module 41; Topic <final_topic>; FullTopic %prefix%/%topic%/; TelePeriod 30'
```
`<final_topic>` = the deploy name (e.g. `airgap_router_pm`, or a spare `s31_spare1`). Module 41 = Sonoff S31
(relay + CSE7766 metering). It re-appears on the bus under `<final_topic>`.

## 6. Verify (dev)
```
mosquitto_pub -h localhost -t 'cmnd/<final_topic>/Status' -m '8'      # force a sensor read
mosquitto_sub -h localhost -t 'tele/<final_topic>/SENSOR' -t 'stat/<final_topic>/#' -v
```
Expect `ENERGY` with `Power`/`Voltage`/`Current`. `tele/<final_topic>/LWT = Online` = connected.

## 7. Register — OR leave as a spare
- **Registered (logs to hot.db):** add to `instance/tasmota-devices.yaml` (`device_id`, `area`, `device_type:
  energy_meter`), then `sudo systemctl restart ha-tasmota-bridge`. Verify the bridge maps it →
  `home/<area>/<device_id>/state` with canonical `power_w/voltage_v/current_a/...`. The registry rides the
  failover-sync manifest (`instance/tasmota-devices.yaml` is `optional|sync`), so standbys + new servers mirror it.
- **Spare (deliberately NOT logged):** flash + get it on the network under `s31_spareN`, but do **NOT** add a
  registry entry. The bridge logs one `telemetry from UNKNOWN Tasmota topic 's31_spareN'` warning and drops it —
  the device sits on the bus, powered/metering, ready to deploy later by adding a registry row + repointing its
  Topic. This is the intended state for staged spares.

## 8. Energy calibration (optional, per deployment)
With a known resistive load ON, over MQTT: `cmnd/<topic>/PowerSet <watts>`, `VoltageSet <mV>`, `CurrentSet <mA>`.
Voltage is usually close from the factory; set Power against a known load if the campaign needs accuracy.

## 9. Later: repoint / graduate a spare
Repoint the Tasmota Topic (`cmnd/<old>/Topic <new>`), add the matching registry row, restart the bridge. For
on/off **control**, the reverse path is `cmnd/<topic>/POWER ON|OFF` — wire it into the control layer when a plug
graduates from meter to actuator (`tasmota_bridge` is read-only today).

---

## Gotchas (all learned on 2026-07-06)
- **3.3 V only**, never 5 V. Check the adapter's voltage jumper.
- **UART is crossed** (adapter TX→S31 RX, RX→S31 TX). A pad reading a steady 3.3 V idle is TX; a *bouncing* pad
  is a floating/unconnected input. Both data pads should read steady 3.3 V when idle + connected.
- **Pogo pins are flaky.** If esptool says "No serial data received": bisect with a **loopback** (jumper adapter
  TX→its own RX, S31 removed; if it echoes, the adapter is fine → the fault is the S31 pad/contact — usually the
  TX pad). Solder a thin wire to a stubborn pad rather than fight pogos.
- **HTTP API is DISABLED by default** (recent Tasmota) — `curl /cm?cmnd=…` returns empty. Config the device
  **over MQTT** once it's on the broker, not HTTP. (Or the user can use the web UI Console/Config pages locally.)
- **`MqttHost` = `192.168.0.210`, not the VIP `.200`** — `.200` is unreachable from CTWap_24g WiFi; a device set
  to `.200` never connects and can't be reached to fix. WiFi Tasmota devices use `.210` like the edge nodes.
- **A device can be fully on the network and NOT registered** — registration (tasmota-devices.yaml) is what makes
  the bridge map + log it; omit it for spares.

Related memory: `mcu-power-cycle-after-flash`, `tasmota-wifi-intake-is-user-step`, `active-network-main-not-airgap`,
`signal-turn-taking-with-hugh`, `plug-g11-s31-deployment`.
