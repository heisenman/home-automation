# Procedure: Flash & configure a Netgear R7800 (Nighthawk X4S) to OpenWrt for the air-gapped HA net

**Status:** validated end-to-end on 2026-07-01 (first unit). This is the *repeatable procedure* distilled from that run —
follow it as a checklist; the discovery/rationale lives in `openwrt-prestage.md`.

**Audience:** run from the **dev node** (`ha-dev`, `.210`) which is dual-homed: `enp4s0` = LIVE net (do NOT disturb —
it holds the VRRP VIP), `eno1`/wifi = the bench router. See memory `dev-node-is-ha-dictator`.

---

## 0. Gotchas that cost us time (read first)
1. **`uhttpd` is used by BOTH stock Netgear firmware AND OpenWrt** — the web-server banner does NOT tell you which is
   running. Fingerprint with the unauthenticated **`http://192.168.1.1/currentsetting.htm`** (stock returns
   `Firmware=V1.0.3.92…Model=R7800…`; on OpenWrt this 404s and SSH:22 is open).
2. **Radio↔band mapping is hardware-specific and can be SWAPPED vs any draft.** On this unit `radio0 = 5 GHz`
   (`…1b500000.pcie…`), `radio1 = 2.4 GHz` (`…1b700000.pcie…`). Never hand-write `option path`; configure wifi
   **in place** on the device and only change ssid/key/network/isolate/hidden/country.
3. **dropbear has no SFTP** → `scp` (which defaults to SFTP) fails with `sftp-server: not found`. Use
   **`cat local | ssh root@host 'cat > /remote'`** (or `scp -O`).
4. **`/usr/sbin` and `/sbin` are not in the dev node's non-login PATH** → `iw`, `wpa_supplicant`, `dhcpcd` look
   "missing". `export PATH=/usr/sbin:/sbin:$PATH` (or call by full path; `sudo` already has them via secure_path).
5. **`pkill -f wpa_supplicant_bench` self-matches** its own command line and kills the shell (exit 144). Put such
   patterns in a **script file**, not an inline command.
6. **Deleting a netns that holds the wifi phy orphans it** (mt7921 vanishes from `/sys/class/ieee80211`). Migrate it
   back FIRST: `ip netns exec <ns> iw phy <phy> set netns 1` before `ip netns del`; recover with
   `modprobe -r mt7921e && modprobe mt7921e`.
7. **Stale `wpa_supplicant` control socket blocks re-association.** Between attempts: kill all `wpa_supplicant` and
   `rm -rf /var/run/wpa_supplicant_bench`.
8. **apk needs a correct clock.** The R7800 has no RTC; a stale clock (defaults to build date) makes apk fail with
   `wget: error 5` / "unexpected end of file" and shows only ~170 packages. **Set the clock first**
   (`ssh root@router "date -u -s '<now>'"`), then `apk update` → ~11k packages.

---

## 1. Prerequisites
- Physical **R7800** (verify the label literally reads `R7800` — "X4S" is reused on brickable look-alikes).
- Dev-node tools: `nmrpflash`, `tftp-hpa`, `iw`, `wpasupplicant`, `dhcpcd-base`, `sshpass`, `nft`
  (`sudo apt-get install -y nmrpflash tftp-hpa iw wpasupplicant dhcpcd-base sshpass nftables`).
- Verified image: `./provisioning/openwrt/fetch-image.sh` → `instance/openwrt/…-factory.img`
  (+ `…-sysupgrade.bin` for later in-place upgrades). Refuses on SHA256 mismatch.

## 2. Identify the unit
```
# dev node on the bench segment
sudo ip addr add 192.168.1.10/24 dev eno1
curl -s http://192.168.1.1/currentsetting.htm    # expect Firmware=V1.0.3.92…Model=R7800 (stock)
```

## 3. Flash (TFTP recovery — the method that worked)
`nmrpflash` (NMRP) is the "blessed" path but its advertising window did not catch this unit; **TFTP recovery did**:
```
# 1) put the router in TFTP recovery: power off, hold the reset pin, power on, keep holding
#    until the POWER LED blinks white (~5–10s), then release. Router listens at 192.168.1.1.
ping -c2 192.168.1.1                                  # confirms recovery mode is up
tftp 192.168.1.1 -v -m octet -c put <…-factory.img> firmware   # ~10 MB, a few seconds
# 2) router writes flash + reboots itself — DO NOT cut power for ~3–5 min.
```
Fallback if web-flash/NMRP is preferred: `sudo nmrpflash -i eno1 -v -f <…-factory.img>` then power-cycle (no reset
held); it advertises for 60 s per attempt.

**Confirm OpenWrt came up:** SSH:22 opens (dropbear), `http://192.168.1.1` serves LuCI, and:
```
ssh root@192.168.1.1 'cat /etc/openwrt_release; cat /tmp/sysinfo/board_name'   # 25.12.4, netgear,r7800
```
Fresh OpenWrt has **no root password** (empty). Board is `netgear,r7800`, target `ipq806x/generic`.

## 4. Apply configuration
Push the four hardware-independent files (fill placeholders first — see §7), via cat-over-ssh:
```
for f in network dhcp system firewall; do cat <staged>/$f | ssh root@192.168.1.1 "cat > /etc/config/$f"; done
```
Configure **wireless in place** (preserves correct paths/bands) — set on the device's own `radioN`/`default_radioN`:
`country=US`, `disabled=0`, and per iface `ssid`, `encryption=psk2`, `key=<PSK>`, `network=lan`, `mode=ap`,
`isolate=0`, `hidden=1`. `uci commit wireless`.
Set root password: `printf '<pw>\n<pw>\n' | passwd root`.
Flip LAN to the final IP **detached** (it drops your session):
```
ssh root@192.168.1.1 "setsid sh -c 'sleep 3; /etc/init.d/network restart; /etc/init.d/dnsmasq restart; \
  /etc/init.d/sysntpd restart; wifi reload' >/tmp/apply.log 2>&1 &"
```
Router is now at **192.168.0.1**.

## 5. Verify over wifi (isolated netns — protects the live net)
Because the dev node shares `192.168.0.0/24` with the live net on `enp4s0`, do all bench work in a netns.
Recipe (see `scratchpad/owrt/netns-wifi-up.sh` for the canonical script):
```
PHY=$(ls /sys/class/ieee80211/ | head -1)
sudo ip netns add bench && sudo iw phy $PHY set netns name bench
# wpa_supplicant conf: ssid + scan_ssid=1 (hidden) + psk; associate; dhcpcd (or static .151)
# checks: DHCP lease in .100–.149, ping/ssh 192.168.0.1, nslookup ha-dev.lan/ha-vip.lan
# TEARDOWN (order matters): kill wpa_supplicant; iw phy $PHY set netns 1; ip netns del bench
```

## 6. Packages (needs temporary internet; final state is air-gapped)
Two options — **fix the clock first either way** (§0.8):
- **Wifi double-NAT** (no cabling): router default route → wifi client `.151` (netns) → veth → dev-node NAT out
  `enp4s0`. Scripts: `uplink-up.sh` / `uplink-down.sh`. Uses `nft` (dev node has no `iptables`).
- **WAN cable** (simpler/robust): plug the router's **WAN** port into the live net, enable `wan` proto dhcp, install,
  then disable `wan`.
```
ssh root@192.168.0.1 "date -u -s '<now>'; apk update"     # expect ~11k packages
apk add <list>                                             # see §8 for the installed set
# then restore air-gap: remove default route, rm /tmp/resolv.conf.d/resolv.conf.auto, dnsmasq reload; tear down NAT
```

## 7. Config placeholders (fill at flash time — NEVER commit real values)
| Placeholder | This deployment |
|---|---|
| SSID | `autohome_airgap`, **hidden**, both bands (was `CTWap_24g` — changed; edge nodes need reprovisioning) |
| PSK | existing WPA2 passphrase |
| Country / TZ | `US` / `America/New_York` (`EST5EDT,M3.2.0,M11.1.0`) |
| `.210` MAC | dev node's `enp4s0` MAC (DHCP reservation) |
| `.245` | **dropped** ("won't move"); future **G11** becomes VRRP BACKUP + NTP peer |
| root pw | = the system password |
DNS names are static `config domain` entries: `ha-dev→.210`, `ha-vip→.200` (so they resolve without a live lease).

## 8. Post-install improvements (applied 2026-07-01)
- **Power:** `scaling_governor=schedutil` on both cores (was `performance` pinned at 1.725 GHz at idle). Persisted in
  `/etc/rc.local`. Idle temps 38–50 °C.
- **Wifi range:** 5 GHz moved to **channel 149 (UNII-3)** → TX power **23→30 dBm** (2.4 GHz already 30 dBm). Reload
  only `radio0` (`wifi reload radio0`) to keep a 2.4 GHz mgmt link up.
- **Stealth:** all 11 LEDs darkened (trigger=none, brightness=0), persisted in `rc.local`.
- **Restore point:** `sysupgrade -b` → `instance/openwrt/r7800-config-backup-YYYYMMDD.tar.gz`.
- **Keyless mgmt:** dev node's `~/.ssh/id_ed25519.pub` added to router `/etc/dropbear/authorized_keys`.
- **Installed toolset:** htop, tcpdump-mini, iperf3, ethtool, mtr, iftop, ip-full, bind-dig, arp-scan, socat, lsof,
  nano, diffutils; luci-ssl, luci-mod-dashboard, luci-app-commands, luci-app-ttyd + ttyd, luci-app-attendedsysupgrade,
  luci-app-statistics + collectd-mod-{cpu,interface,load,memory,network,uptime,wireless,thermal}. Overlay ~10% used.
  (`collectd` runs; `ttyd` instance still needs LuCI config.)
- **Clock persistence (no RTC):** boot-restore block in `/etc/rc.local` restores `/etc/last_time` if newer than the
  reset clock; busybox `cron` saves the epoch hourly (guarded to year ≥ 2026 so a bad clock is never persisted). Bridges
  the boot→NTP gap; NTP from `.210` still corrects it precisely once on-segment.
- **Remote logging → `.210`:** router `system.@system[0].log_ip=192.168.0.210 log_proto=udp log_port=514` →
  `logread -r` forwards. Receiver = **rsyslog on the dev node** (`/etc/rsyslog.d/30-r7800-remote.conf`, UDP 514) routing
  by source IP `192.168.0.1` (and hostname) into **`/var/log/r7800.log`** with weekly logrotate. Delivery begins at
  cutover (router can't reach `.210` from the isolated bench). Optional tidy-up: `log_hostname='ha-router'` so remote
  lines are tagged by name rather than the default `OpenWrt`.

## 9. Cutover & rollback
Cutover is a **plug-swap**: pull the current router, plug `enp4s0` into the R7800 — no config changes (it's already
`192.168.0.1`). Then add the **G11** as VRRP BACKUP + NTP peer when it arrives. **Rollback = plug the old router back
in.** Router has no RTC — it self-corrects time via NTP from `.210` once on the same segment.
