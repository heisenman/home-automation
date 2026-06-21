# Stage 2 — Full Server Spec (executed on-device by an LLM)

**You are an LLM with shell + sudo on a freshly-bootstrapped GMKtec G11 (Debian 13 minimal).**
Configure it into a complete home-automation server matching production (.245), reproducibly, ready
to run air-gapped. Work top-to-bottom. Each step has a **Verify** gate — do not proceed until it
passes. Steps note their **offline equivalent** so this same doc provisions the air-gapped failover.

### Operating rules
- **Idempotent:** assume steps may be re-run. Check-before-change; don't blindly re-create.
- **Confirm destructive actions** (partitioning, `mkfs`, overwriting `instance/`) with the human first.
- **Stop on Verify failure.** Diagnose, fix, re-verify. Don't paper over.
- **PII never enters git.** `instance/devices.yaml` (real MACs) and `instance/weather.env` (lat/lon)
  arrive by sneakernet, not clone. `instance/` is git-ignored.
- This repo's conventions: `install.sh` templates the repo path into systemd units; services are
  `ha-*`; data lives under `instance/db/`. Read `docs/adr/` for the "why" behind decisions.
- Record anything non-obvious you discover (e.g. actual NIC chipset) back into this file via PR.

---

## 1. Hardware inventory & driver verification

```bash
sudo lspci -nnk            # CPU/iGPU, NIC, NVMe — note NIC kernel driver in use
sudo lsusb                 # confirm the TP-Link UB500 (RTL8761B) BLE dongle is present
lsblk -o NAME,SIZE,MODEL   # the two NVMe disks
ip -br link                # both 2.5GbE interfaces
uname -r                   # expect 6.12.x
```

- **NIC:** confirm the dual-2.5GbE chipset. If `lspci -nnk` shows Realtek `RTL8125` bound to `r8169`
  and the link is stable at 2500 Mb/s (`ethtool <iface>`), **do nothing**. Only if you see drops /
  flapping / stuck at 1 Gb, build the `r8125` DKMS driver (source carried in the offline bundle, see
  `03-sneakernet-updates.md` §drivers). If it's Intel `i226`, in-tree `igc` is fine.
- **BLE:** the **UB500 dongle is the BLE radio** (`hci0` via `btusb`). Confirm `hciconfig -a` shows it
  UP. Leave onboard Wi-Fi/BT **disabled** (don't load/use it) — avoids the MediaTek-on-Linux risk.

**Verify:** both NICs enumerate, the UB500 shows as an HCI device, both NVMe disks visible.

---

## 2. Base OS configuration

```bash
sudo hostnamectl set-hostname <hostname>            # e.g. ha-prime
sudo timedatectl set-timezone Etc/UTC
```

**Static IP** on the primary 2.5GbE (Debian uses ifupdown or systemd-networkd; pick networkd):
```bash
# /etc/systemd/network/10-lan.network   (adjust iface name + addresses to your LAN)
sudo tee /etc/systemd/network/10-lan.network >/dev/null <<'EOF'
[Match]
Name=enp1s0
[Network]
Address=192.168.0.245/24        # take over the production IP at cutover, or use a new one until then
Gateway=192.168.0.1
DNS=192.168.0.1
EOF
sudo systemctl enable --now systemd-networkd
```

**Air-gap posture:** disable any automatic updates and time-sync-to-internet once cut over.
```bash
sudo systemctl disable --now unattended-upgrades 2>/dev/null || true
# (Keep NTP while online; switch to an RTC/LAN time source when air-gapped.)
```

**Verify:** `hostnamectl`, `ip -br addr` shows the static IP, `ping -c1 <gateway>` works,
`timedatectl` shows UTC.

---

## 3. Storage layout (dual NVMe)

Decide with the human:
- **(A) Split** — NVMe-A = OS (already installed), NVMe-B = data (`instance/db/` + parquet). Simple, more usable space.
- **(B) Mirror** — `mdadm` RAID1 across both for data resilience (less space, survives one disk).

Default to **(A) split** unless the human wants RAID1. Example for (A):
```bash
# CONFIRM the device is the empty second NVMe before touching it!
lsblk
sudo mkfs.ext4 -L ha-data /dev/nvme1n1
sudo mkdir -p /srv/ha-data
echo 'LABEL=ha-data /srv/ha-data ext4 defaults,noatime 0 2' | sudo tee -a /etc/fstab
sudo mount -a
sudo chown visko:visko /srv/ha-data
```
The app's `instance/db/` will live on this disk (symlink or bind-mount in step 7), so SQLite WAL and
daily Parquet compaction get the dedicated NVMe.

**Verify:** `df -h /srv/ha-data` mounted, writable as `visko`, survives `sudo mount -a` cleanly.

---

## 4. System packages (apt now / snapshot-mirror later)

```bash
sudo apt update
sudo apt install -y \
  git curl ca-certificates build-essential pkg-config \
  python3 python3-venv python3-dev \
  mosquitto mosquitto-clients \
  bluez bluetooth libdbus-1-dev \
  ethtool rsync
```
**Offline equivalent:** point `apt` at the local snapshot mirror (`03-sneakernet-updates.md` §apt);
the package list is identical.

**Source-compile candidates** (only if a real need — version/tuning — arises; default to apt):
- **BlueZ** — compile to pin a version / enable the `--experimental` passive-scan path cleanly.
- **mosquitto** — compile to pin/trim.
- **kernel** — optional `znver1`-tuned rebuild (low priority; in-tree 6.12 is already good here).
See `03-sneakernet-updates.md` §source for the vendored-tarball + checksum workflow.

**Verify:** `python3 --version` (3.13.x), `mosquitto -h`, `bluetoothctl --version`, `git --version`.

---

## 5. BlueZ — enable experimental (passive scan or_patterns)

The scanner uses passive BLE with `or_patterns`, which needs BlueZ's experimental APIs.
```bash
sudo mkdir -p /etc/systemd/system/bluetooth.service.d
sudo tee /etc/systemd/system/bluetooth.service.d/experimental.conf >/dev/null <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/libexec/bluetooth/bluetoothd --experimental
EOF
sudo systemctl daemon-reload
sudo systemctl restart bluetooth
sudo systemctl enable bluetooth
```
(Verify the `bluetoothd` path with `systemctl cat bluetooth` — Debian uses `/usr/libexec/bluetooth/bluetoothd`.)

**Verify:** `systemctl show bluetooth -p ExecStart | grep -q experimental` and `bluetoothctl show`
lists the UB500 controller as `Powered: yes`.

---

## 6. Python venv (pinned, reproducible)

```bash
cd ~/home_automation
python3 -m venv venv
# ONLINE:
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt
# OFFLINE: venv/bin/pip install --no-index --find-links /path/to/wheelhouse -r requirements.txt
```
> **cp313 note:** `requirements.txt` was pinned on cp312. On Debian 13's Python 3.13, bump
> `pyarrow==17.0.0` → `18.x` (17 predates cp313 wheels) and re-verify `duckdb` has a cp313 wheel; pip
> will report any package lacking a 3.13 wheel. Rebuild the wheelhouse on a 3.13 twin so online/offline match.

**Verify:**
```bash
venv/bin/python -c "import bleak, duckdb, pyarrow, fastapi, uvicorn, paho.mqtt, yaml, httpx, uvloop; print('deps OK')"
```

---

## 7. Deploy the app (services, config, sudoers)

**a. Instance config (sneakernet — PII):** copy the real `instance/devices.yaml` and
`instance/weather.env` from the transfer USB (or `rsync` from .245) into `~/home_automation/instance/`.
Never `git pull` these — they're git-ignored.

**b. Point data dir at the data NVMe** (from step 3):
```bash
# move/relocate instance/db onto /srv/ha-data and symlink
mkdir -p /srv/ha-data/db
ln -sfn /srv/ha-data/db ~/home_automation/instance/db
```

**c. systemd units:** `install.sh` templates this repo's path into the units and installs them.
```bash
cd ~/home_automation
sudo ./install.sh         # templates REPO_DIR, installs ha-*.service/.timer, daemon-reload
```
Review what it enables: `ha-writer`, `ha-scanner` (Type=notify, WatchdogSec=120), `ha-api`,
`ha-compactor`(+timer 02:00 UTC), `ha-verify-hashes`(+timer Sun 03:00 UTC), `ha-weather`(+15min timer).

**d. mosquitto config:** install the repo's broker drop-in.
```bash
sudo cp server/config/mosquitto.conf /etc/mosquitto/conf.d/ha.conf
sudo systemctl restart mosquitto && sudo systemctl enable mosquitto
```

**e. Narrow sudoers** — REPLACE the broad bootstrap rule with the project rule:
```bash
sudo tee /etc/sudoers.d/ha-services >/dev/null <<'EOF'
visko ALL=(ALL) NOPASSWD: /usr/bin/systemctl start ha-*, /usr/bin/systemctl stop ha-*, \
 /usr/bin/systemctl restart ha-*, /usr/bin/systemctl enable ha-*, /usr/bin/systemctl disable ha-*, \
 /usr/bin/systemctl daemon-reload, /usr/bin/systemctl start mosquitto, \
 /usr/bin/systemctl stop mosquitto, /usr/bin/systemctl restart mosquitto
EOF
sudo visudo -cf /etc/sudoers.d/ha-services      # syntax check — MUST pass
sudo rm -f /etc/sudoers.d/90-visko-bootstrap    # remove the broad bootstrap grant
```

**f. Enable + start services:**
```bash
sudo systemctl enable --now ha-writer ha-scanner ha-api
sudo systemctl enable --now ha-compactor.timer ha-verify-hashes.timer ha-weather.timer
```

**Verify:** `systemctl is-active ha-writer ha-scanner ha-api mosquitto bluetooth` all `active`;
`journalctl -u ha-scanner -n50` shows BLE advertisements arriving; `sudo -n visudo` rule present.

---

## 8. Data migration from .245

While both boxes are online (pre-cutover), copy the historical data:
```bash
# DBs + Parquet archive (stop the writer briefly on the SOURCE for a clean hot.db copy, or use .backup)
rsync -av visko@192.168.0.245:/home/visko/home_automation/instance/db/  /srv/ha-data/db/
rsync -av visko@192.168.0.245:/home/visko/home_automation/instance/weather.env ~/home_automation/instance/
# weather.db if separate:
rsync -av visko@192.168.0.245:/home/visko/home_automation/instance/db/weather.db /srv/ha-data/db/ 2>/dev/null || true
```
Restart services after the copy.

**Verify:** the API returns historical rows:
`curl -s localhost:8123/devices | head` and a 30-day deep-query returns the migrated range.

---

## 9. Verification checklist (the box is "done" when all pass)

- [ ] `systemctl is-active ha-writer ha-scanner ha-api mosquitto bluetooth` → all `active`
- [ ] `journalctl -u ha-scanner` shows live SwitchBot advertisements (battery/temp decoding)
- [ ] `curl -s localhost:8123/devices` lists the meters; dashboard loads at `http://<ip>:8123/`
- [ ] `mosquitto_sub -h localhost -t 'home/#' -v` shows published readings
- [ ] Weather: `curl -s 'localhost:8123/weather/meta'` → `available: true`
- [ ] Timers scheduled: `systemctl list-timers 'ha-*'`
- [ ] Reboot test: `sudo reboot`; after boot all services come back, scanner resumes
- [ ] BLE history tool works on this box: `venv/bin/python tools/switchbot_history.py --device <mac> --device-type switchbot_meter_outdoor --window 40 --dry-run`

---

## 10. Sneakernet readiness & failover

- Read `03-sneakernet-updates.md` and stage the first **offline update bundle** (apt snapshot,
  wheelhouse, source tarballs, git bundle) so this box can be updated with the LAN unplugged.
- Switch the weather lane to the **air-gap source** when going offline (replace `OpenMeteoSource` in
  `server/weather/__main__.py:build_source` with the transfer-reading source; data syncs in/out during
  backup — see `docs/adr/ADR-0008-weather-lane.md`).
- **Failover unit:** provision the second G11 with the same bootstrap ISO + this spec, then keep it in
  parity by applying the same bundles and `rsync`-pulling `/srv/ha-data/db/` from prime on a timer.
  Promotion = move the HA IP/hostname to the failover and `systemctl enable --now` its services.

---

### Cutover from .245
Only after the checklist passes and data is migrated: stop services on .245, move the LAN IP
(`192.168.0.245`) to the G11 (or update the DHCP reservation / the dashboard bookmark), confirm edge
nodes (ESP32s) reconnect to the broker, then retire .245. Keep .245 powered-off-but-intact as a
rollback for a few days before repurposing.
