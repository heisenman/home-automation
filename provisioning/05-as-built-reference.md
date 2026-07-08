# As-built reference — the `.210` dictator (captured 2026-07-08)

**Read this first when provisioning the next box.** The rest of this directory (`01`…`04`) was
written *before/during* the first G11 build and still carries speculative "we'll probably…" language
and open trials. This page is the opposite: a snapshot of the **actual, battle-tested state** of the
running dictator after ~2 weeks in production, so the incoming failover unit comes up matching it
instead of re-discovering everything the hard way.

> Provenance: captured live from `192.168.0.210` (hostname `ha-dev`) on 2026-07-08 via the commands in
> §H. When any of `01`…`04` disagrees with this page, **this page wins** for present state; those pages
> remain the procedural walkthrough and the record of *why*.

---

## A. Hardware — CONFIRMED (resolves the open questions in README/§1)

| Component | As-built fact | Consequence for the plan |
|---|---|---|
| Board | GMKtec NucBox **G11** (AMD **Ryzen Embedded R2514**, 4C/8T) | `amd64` + `amd64-microcode`; no Intel ucode. Identical SKU for the failover unit. |
| NIC | **Dual Realtek RTL8125** `[10ec:8125]`, **both on in-tree `r8169`** (kernel 6.12) | **Stable for weeks — no `r8125-dkms` needed.** Primary = **`enp4s0`**; `eno1` is the 2nd port (down). Drop the "maybe build DKMS" hedge. |
| Wi-Fi | Onboard **MediaTek MT7922** (`mt7921e`) as `wlp2s0` — **down/unused** | Server is wired. Leave Wi-Fi down. |
| **BLE** | **Onboard MT7922 Bluetooth** (`hci0`, internal USB `0e8d:0616`) — **this is the production radio** | **The plan's caution to avoid the MediaTek BT is superseded by reality: it has run the passive `or_patterns` scanner for weeks (227 MB RX / 4M events, no watchdog stalls). No UB500 dongle was ever fitted.** Requires **`firmware-mediatek`** (see §C — this is the package the old plan omitted). The UB500 (RTL8761B) remains a known-good *fallback* only. |
| Storage | **Single NVMe** — WD SN740 238 GB. Layout: `p1` 976 MB ESP · `p2` 225.6 GB ext4 `/` · `p3` 12 GB swap | **The dual-NVMe split/mirror (spec §3) does NOT apply.** `instance/db/` lives on the OS disk; there is no `/srv/ha-data`. If the failover box ships with a 2nd NVMe, §3 becomes live again — otherwise skip it. |

---

## B. OS & toolchain versions (the pin targets)

| | Version |
|---|---|
| Distro | **Debian 13 "trixie"** (amd64, minimal) |
| Kernel | **6.12.94+deb13-amd64** |
| Python | **3.13.5** (system + venv). `requirements.txt` already bumped to cp313 wheels (`pyarrow 18.1.0`). |
| Node | **v22.23.0** (npm 10.9.8) — installed by `firstboot.sh` |
| Claude Code | 2.1.187 |
| BlueZ | **5.82** (with the `--experimental` drop-in, §D) |
| mosquitto | Debian trixie build |
| sqlite3 CLI | 3.46.1 |
| git / gh | 2.47.3 / `gh` present |

---

## C. Package set actually in use

Curated from `apt-mark showmanual` on the live box, grouped by purpose. **Bold = required but NOT in
the current `stage2-finish.sh` PKGS array / `install.sh`** — these are the gaps that made the first
build bumpy.

```
# app + broker + BLE core (covered by install.sh / stage2-finish.sh)
git curl ca-certificates build-essential pkg-config
python3 python3-venv python3-dev python3-pip
mosquitto mosquitto-clients
bluez bluetooth libdbus-1-dev
ethtool rsync

# firmware — REQUIRED for the onboard radios (BLE won't come up without mediatek)
firmware-mediatek firmware-realtek firmware-amd-graphics

# cluster / infra layer — installed & configured BY HAND last time (see §F, §G)
keepalived            # VRRP: floats the VIP .200
chrony                # LAN NTP server (replaces client-only timesyncd) — provisioning/ntp/
ntfy                  # self-hosted push server on :8095 — provisioning/ntfy/

# operator + tooling
gh sqlite3 nftables iproute2 iw wpasupplicant dhcpcd-base
powertop sysstat lsof usbutils pciutils dmidecode

# source-compile toolchain (only if you rebuild bluez/mosquitto/kernel — spec §4)
cmake ninja-build ccache bison flex gperf

# device flashing side-tools (Tasmota/OpenWRT/edge work; optional)
dfu-util nmrpflash tftp-hpa sshpass
```

`firmware-mediatek`, `keepalived`, `chrony`, `ntfy` are the four that were never in the reproducible
path — install them explicitly on the next box (§G lists the config each still needs).

---

## D. systemd unit inventory (what "fully running" looks like)

`install.sh` templates the repo path into every unit under `systemd/` and installs them, so the
**app-layer** set below is reproducible today. The **infra** set is not (see §G).

**App services (`install.sh`):** `ha-writer` · `ha-scanner` (BLE, Type=notify + WatchdogSec) ·
`ha-api` (:8123) · `ha-api-tls` (:8443) · `ha-controller` (actuator control — **VIP-gated**, only
truly active on the VIP holder) · `ha-cluster-heartbeat` · `ha-edge-mapper` · `ha-edge-history` ·
`ha-reconcile-history` · `ha-relay-coordinator` · `ha-tasmota-bridge` · `ha-levoit-bridge` ·
`ha-ntfy-bridge`.

**App timers (`install.sh`):** `ha-compactor` (02:00Z) · `ha-verify-hashes` (Sun 03:00Z) · `ha-weather`
(15 min) · `ha-gap-watcher` · `ha-power-sampler` · `ha-rollup` · `ha-mesh-probe`.

**Wake/agent:** `ha-agent-wake@dev` (the coordination wake-watcher — headless dev runner).

**Infra (NOT installed by the repo scripts — set up by hand, §G):** `mosquitto` · `bluetooth`
(with `/etc/systemd/system/bluetooth.service.d/experimental.conf` → `bluetoothd --experimental`) ·
`keepalived` · `chrony` · `ntfy`.

> The old §7 prose lists only 6 services — that's stale. The source of truth is `ls systemd/` plus the
> five infra units above.

---

## E. Connection paths & ports

| Path | Value | Notes |
|---|---|---|
| Static IP | **`192.168.0.210/24`** on `enp4s0` via **ifupdown** (`/etc/network/interfaces`) | Not systemd-networkd. gw `192.168.0.1`, DNS `192.168.0.1`, search `lan`. |
| **Cluster VIP** | **`192.168.0.200/24`** — floated by keepalived onto the current dictator | This is what devices/clients should target for control. Held by `.210` today. |
| MQTT broker | **`192.168.0.210:1883`**, `allow_anonymous true`, `message_size_limit 65536` | **Devices connect to `.210`, not the VIP `.200`** (VIP is unreachable from the CTWap Wi-Fi). Anonymous-on-LAN is the current posture (see the open `broker-auth-posture` board task). |
| HTTP API | `:8123` (plain), `:8443` (TLS) | Dashboard + `/api/v1/*`. Catalog edits need BOTH restarted. |
| ntfy | `:8095` | Self-hosted push; `ha-ntfy-bridge` forwards `home/_alert/new` → ntfy. |
| NTP | `:123` (chrony, `allow` the LAN) | Dictator is the LAN time anchor once air-gapped (E1001 SNTP → `.210`). |
| Repo | `https://github.com/heisenman/home-automation.git` → `~/home_automation` | `instance/` is git-ignored (PII + data + dictator-local registries). |

---

## F. Cluster / failover topology (the incoming box's real job)

### F.1 Two intake modes — decide this before Stage 2

**The install media is scenario-agnostic and loads NO instance data.** Stage 1 (the preseed) produces
an identical bare box for every unit — Debian + repo clone, zero secrets. PII (`instance/devices.yaml`
real MACs, `instance/weather.env` lat/lon) and the data DBs are git-ignored and **never** touch the ISO
or git. *How* the data of record arrives is a **Stage-2** decision, and it forks:

| | **Mode A — greenfield** (first box / no existing record-keeper) | **Mode B — join an existing system** (e.g. `ha-2` joining `.210`) |
|---|---|---|
| Device registry | sneakernet `devices.yaml` + `weather.env` onto the box (spec §7a) | comes with the config-of-record pull (below) |
| Historical data | none — starts fresh; weather backfilled from Open-Meteo (spec §8.1) | **pulled from the current record-keeper** |
| How | manual copy + `install.sh` | **`failover/provision-peer.sh --from <dictator>`** |
| Gate | weather reaches ≥ Parquet archive start | **hard archive-parity gate** before the box is dictator-eligible |

**Why the data is NOT loaded during the d-i install** (a deliberate choice, not an omission): the ISO
carries no secrets/creds by design (the `dictator-files.manifest` even marks some files `preposition` =
*never sent over the wire*); the archive pull is a months-deep, resumable, **parity-gated** reconcile
that must not run in a `late_command`; and it needs the venv + the `id_cluster` SSH back-channel, which
only exist post-install. So data intake is a first-class **Stage-2** step (see the finisher's step 5),
not part of the image. **`ha-2` is Mode B** — pull from `.210` via `provision-peer.sh`.

### F.2 Cluster / VIP

The `.210` box is the VRRP **MASTER**. The failover pair and all its tooling already exist under
`failover/` — the provisioning docs simply never linked to it. Present config:

- **keepalived** `HA_DICTATOR` instance on `enp4s0`, `virtual_router_id 51`, VIP `192.168.0.200/24`.
  Primary `.210`: `state MASTER`, `priority 150`. Standby: `state BACKUP`, `priority 100`
  (`failover/keepalived.conf.tmpl` fills `@STATE@`/`@PRIORITY@`/`@IFACE@` from `instance/cluster.env`).
- Health gating: `failover/healthcheck.sh` (`chk_dictator`, `weight -40` — an unfit dictator drops
  below the peer and fails over). Transitions run `failover/notify.sh` (`$3=MASTER` → fence peer +
  start `ha-controller`; `BACKUP/FAULT` → stop controller). `preempt_delay 30` debounces flapping.

**Bringing the NEW server up as the peer** (this is the smooth path the plan was missing):

1. Provision it bare-metal via Stage 1 (`01-bootstrap-iso.md`) + Stage 2 (`stage2-finish.sh`) so the
   **app layer** is running, then add the **infra layer** (§G).
2. Give it a distinct hostname + a free static IP (e.g. `.211`), point `instance/cluster.env` at the
   pair, render `keepalived.conf.tmpl` as `BACKUP`/`100`.
3. Elevate it to record-keeping status with **`failover/provision-peer.sh --from 192.168.0.210`** —
   this syncs config-of-record + hot tier + the months-deep parquet archive and **hard-gates on
   archive parity** (the guard that prevents a thin-archive box from silently becoming dictator, per
   the 2026-06-25 incident). See `failover/README.md` + `failover/failover-runbook.md`.

> Per the standing plan, the inbound NUC becomes the next **dictator** via a `210 ⇄ new-box` reconcile
> pair (not a `.245` drill). `.245` (Hugh's fileserver) is retired from the HA role once the new box is
> validated and holds the full archive.

---

## G. What the current install flow does NOT yet reproduce — do these by hand on the next box

The app layer is scripted; these five host-level pieces were hand-built on `.210` and must be repeated
(commands/assets already in-repo):

1. **`firmware-mediatek`** — `sudo apt install -y firmware-mediatek firmware-realtek`. Without it the
   onboard MT7922 BLE radio (§A) never enumerates `hci0`; the scanner has nothing to bind.
2. **keepalived + VIP** — `sudo apt install -y keepalived`, render `failover/keepalived.conf.tmpl`
   (BACKUP/100 for the new peer), `sudo systemctl enable --now keepalived`. Do this **after**
   `provision-peer.sh` passes the parity gate, so a thin box never claims the VIP.
3. **chrony (LAN NTP)** — `provisioning/ntp/install-ntp-serve.sh` (installs chrony, drops
   `chrony-ha-serve.conf`, disables `systemd-timesyncd`). Required for air-gap time.
4. **ntfy server** — `provisioning/ntfy/` (`sudo apt install -y ntfy`, drop `server.yml`, enable on
   `:8095`). `ha-ntfy-bridge` (app layer) expects it.
5. **Narrow sudoers** — the live box still carries the broad `90-visko-bootstrap` grant plus a
   `ha-host-leds` rule; the narrow `ha-services` rule from spec §7e was never applied. keepalived's
   `notify.sh` needs `NOPASSWD: systemctl … ha-*` — fold that in when you narrow it.

Folding items 1–4 into `stage2-finish.sh` (guarded, config-aware) is the obvious next improvement so
the failover box needs even less by hand than this list.

---

## H. How this snapshot was captured (refresh procedure)

Re-run on the dictator to regenerate this page after material changes:

```bash
. /etc/os-release; echo "$PRETTY_NAME  kernel $(uname -r)"
lspci -nnk | grep -iA2 -E 'ethernet|network'      # NIC + driver
hciconfig -a | head -3                             # BLE radio
lsblk -o NAME,SIZE,MODEL,MOUNTPOINT                # disks
ip -br addr; ip route show default                 # IPs incl. VIP + gateway
comm -23 <(apt-mark showmanual|sort) <(apt-mark showauto|sort)   # manual packages
systemctl list-unit-files | grep -iE 'ha-|keepalived|chrony|ntfy|mosquitto|bluetooth'
ss -ltnp | grep -E ':1883|:8123|:8443|:8095|:123'  # listeners
sudo cat /etc/network/interfaces /etc/keepalived/keepalived.conf /etc/mosquitto/conf.d/*.conf
```
