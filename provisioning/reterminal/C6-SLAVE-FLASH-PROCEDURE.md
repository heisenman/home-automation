# reTerminal D1001 — physically flashing the ESP32-C6 slave (serial/UART)

**Why this exists:** Spike 0 (2026-07-01) proved the P4 host BLE stack works, but the **factory C6 NCP
firmware is esp_hosted 2.3.0**, which (a) doesn't share BT with the host (`bt_controller_init → 0x106
NOT_SUPPORTED`) and (b) is **too old for over-SDIO slave-OTA** (that feature needs slave **> 2.5.X**; our
`cmd/slaveota` transferred all 1.13 MB then failed `0x106` at finalize). So the C6 must be flashed by a
**version-independent serial method** — once. After this, the slave is 2.12.9 (matched to the host) and all
future updates CAN use the over-SDIO `cmd/slaveota` path.

**Goal:** flash the pre-built matched slave (`CONFIG_ESP_HOSTED_CP_BT=y` + WiFi) onto the C6, then confirm
`cmd/ble on` → `running:true` + adverts.

---

## What you need
- Your programmer (ESP-Prog or any 3V3 USB↔UART) + pogo pins.
- The reTerminal on USB-C to the desktop (P4 = `/dev/ttyACM0`).
- Pre-built C6 slave firmware at **`~/reterminal-dev/c6-slave/build/`** (already built this session;
  rebuild recipe at the bottom if the tree is gone).
- ESP-IDF sourced: `source ~/esp/esp-idf/export.sh`

## C6 signals to reach (cross-ref the Seeed reTerminal D1001 schematic for the actual pads/test-points)
The ESP32-C6 serial-download interface:
| Signal | C6 pin | Connect to programmer |
| --- | --- | --- |
| UART0 TX | **GPIO16** (U0TXD) | programmer RX |
| UART0 RX | **GPIO17** (U0RXD) | programmer TX |
| Boot strap | **GPIO9** | programmer DTR/IO0 (or hold LOW manually at reset → download mode) |
| Reset | **EN / CHIP_PU** | programmer RTS/EN (or pulse LOW manually) |
| Ground | GND | GND |
| Power | — | **DO NOT connect VDD** — board is self-powered over USB-C; back-feeding fights the rail |

> Seeed may label these on a `PROG_C6`-style header or as bare test-points. The signals above are what
> matter; find where they land on the D1001.

---

## Procedure

### 1. Park the P4 so it stops driving the C6
The P4 host normally holds/reboots the C6 via **P4 GPIO13 (slave reset)** and drives the SDIO bus — it will
fight the programmer. Put the P4 into its ROM bootloader (firmware not running, GPIO13 released):
```sh
esptool.py -p /dev/ttyACM0 --before default_reset --after no_reset --no-stub chip_id
```
Leave it there (don't power-cycle) for the whole flash. (Or: hold the P4 **BOOT**, tap **RST**, release BOOT.)

### 2. Wire the programmer to the C6 pads (pogo). VDD NOT connected.

### 3. Back up the factory C6 first (discipline — image before you overwrite)
Enter C6 download mode (ESP-Prog auto-resets via DTR/RTS; with bare pogo pads, hold GPIO9 LOW and pulse EN),
then:
```sh
esptool.py -p /dev/ttyUSB0 --chip esp32c6 --before no_reset --after no_reset \
  read_flash 0x0 0x400000 ~/c6-factory-2.3.0-backup.bin
```
(`/dev/ttyUSB0` = your programmer's port; C6 is 4 MB. Keep this backup off-git next to the P4 factory image.)

### 4. Flash the matched 2.12.9 slave (full image: bootloader + parttable + otadata + app)
```sh
cd ~/reterminal-dev/c6-slave
source ~/esp/esp-idf/export.sh
idf.py -p /dev/ttyUSB0 flash
```
If `idf.py flash` can't auto-enter download mode (pogo rig without auto-reset wiring), enter download mode
manually and flash with esptool directly:
```sh
cd ~/reterminal-dev/c6-slave/build
esptool.py -p /dev/ttyUSB0 --chip esp32c6 -b 460800 --before no_reset --after hard_reset \
  write_flash --flash_mode dio --flash_freq 80m --flash_size 4MB \
  0x0 bootloader/bootloader.bin \
  0x8000 partition_table/partition-table.bin \
  0xd000 ota_data_initial.bin \
  0x10000 network_adapter.bin
```

### 5. Reconnect + reset the whole reTerminal
Remove the pogo pins, then reset so the P4 exits its bootloader and re-inits the C6.
**⚠️ The D1001 has an onboard battery — unplugging USB-C does NOT power it off** (the battery holds it up).
Use the physical **RST/reset button** (or let the battery drain) for a true cold boot; a USB re-plug alone
may only trigger a reset via the charge circuit, not a full power-down. Either way the panel boots
`v27-slaveota` and esp_hosted handshakes the **2.12.9** slave. (Verified 2026-07-01: the C6 also re-inits
cleanly from scratch on every `cmd/slaveota` activate-reboot, so a full cold boot isn't strictly required.)

### 6. Verify BLE (the payoff)
```sh
mosquitto_sub -h 192.168.0.210 -t 'd1001-beachhead/#' -v          # watch
mosquitto_pub -h 192.168.0.210 -t d1001-beachhead/cmd/ble -m on   # start observer
```
On `d1001-beachhead/ble`:
- ✅ **`running:true` + `adv_total` climbing** → the whole edge-node initiative is unblocked (host+slave both
  2.12.9 — the version mismatch is retired too).
- No SMA antenna yet: hold a BLE beacon (a SwitchBot meter, or a phone advertising) **right against the panel**
  for the first test. Range/gateway-coverage is the separate antenna task.

If `running:false` again with `0x106` → the flash didn't take (re-check download mode / wiring).

---

## Recovery
The C6 is re-flashable via the same UART indefinitely. To revert: `write_flash 0x0 ~/c6-factory-2.3.0-backup.bin`
(the full 4 MB image from step 3). A bad flash never bricks permanently — worst case re-enter download mode
and re-flash.

## Rebuild the slave firmware (if `~/reterminal-dev/c6-slave` is gone)
```sh
cp -r ~/reterminal-dev/d1001-beachhead/managed_components/espressif__esp_hosted/slave ~/reterminal-dev/c6-slave
cd ~/reterminal-dev/c6-slave && source ~/esp/esp-idf/export.sh
idf.py set-target esp32c6 && idf.py build     # stock C6 defaults already give CP_BT=y + CP_WIFI=y
# verify: grep -m1 '^CONFIG_ESP_HOSTED_CP_BT=' sdkconfig   -> =y
```
(A copy of `network_adapter.bin` is also staged at `~/reterminal-dev/d1001-beachhead/build/` for the future
over-SDIO `cmd/slaveota` path, which works once the slave is ≥ 2.6.0.)
