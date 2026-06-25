# OpenWRT pre-stage — Netgear R7800 (board `openwrt-prestage`, theme B)

**Purpose:** prepare everything for the incoming router OFFLINE so the live cutover (`openwrt-router-onboard`,
HUGH-gated, needs a maintenance window) is fast and reversible. This is the standalone prep half — it does
**not** touch the live network and does **not** need the cutover gate cleared.

**Owner:** ops (taken over from dev per Hugh, 2026-06-24 — standalone, no shared files).
**Status:** image pinned + offline configs drafted. Awaits the physical router (ETA ~2026-07-09) to flash.

## 0. Hardware — CONFIRMED
- **Netgear Nighthawk X4S = Netgear R7800**, Qualcomm **ipq806x**, OpenWRT profile **`netgear_r7800`**
  (well-supported, DSA switch). Confirmed by Hugh 2026-06-25 → STEP 0 cleared.
- ⚠️ The "X4S" name is also used by unrelated units (D7800 DSL modem, EX7500 extender). **Before flashing,
  eyeball the label: it must read `R7800`.** Wrong rev bricks it.

## 1. Image — pinned + verified
OpenWRT **25.12.4** (latest stable, 2026-05). Run the fetch script; it downloads to the gitignored
`instance/openwrt/` and refuses on a SHA256 mismatch (binaries are never committed):

```
./provisioning/openwrt/fetch-image.sh
```

| image | use | sha256 |
|---|---|---|
| `…netgear_r7800-squashfs-factory.img`   | first flash from **stock Netgear** firmware / TFTP recovery | `08a3cec5…dc5ee74` |
| `…netgear_r7800-squashfs-sysupgrade.bin`| later upgrades from a **running OpenWRT** (keeps settings)  | `db791a5d…8cf73cf` |

(Full checksums live in `fetch-image.sh`. To re-pin a newer release: `OPENWRT_VER=x curl … sha256sums | grep r7800`.)

## 2. Target topology (the design, and why)
End state: the R7800 is the **air-gapped HA network's** router. Constraints that shaped it:
- **Don't disrupt the `.245` fileserver / SMB clients.** → keep the **same `192.168.0.0/24`**, same gateway
  `.1`; nothing re-addresses. SMB is intra-LAN, unaffected by the air gap.
- **Fix `vip-unreachable-from-wifi`.** → **one bridged L2 segment** (`br-lan` = LAN ports + both wifi
  radios), **AP isolation OFF**. ARP + keepalived gratuitous-ARP for the floating **VIP `.200`** then cross
  the bridge, so wifi edge nodes follow a dictator failover. **No VLAN split of the HA devices** — that
  would re-break exactly this. (A guest/Internet VLAN is possible later if a WAN uplink is kept; out of scope.)
- **Air gap.** → WAN disabled by default; dnsmasq resolves LAN names locally; the router serves **NTP** to
  the LAN and peers `.210`/`.245` as the time source (R7800 has no RTC — see `etc/config/system`).

Address plan: `.1` router · `.100–.149` DHCP pool · `.150–.199` static infra/edge nodes · **`.200` VIP
(VRRP, never a lease)** · `.210` ha-dev · `.245` fileserver.

Draft configs (ready to drop on the device, secrets/paths as `__PLACEHOLDERS__`):
`provisioning/openwrt/etc/config/{network,dhcp,wireless,firewall,system}`. **Secrets — MACs (`.210`/`.245`/
edge), the wifi PSK, the country code — are NOT committed; fill them at flash time.** Keeping the existing
SSID `CTWap_24g` + passphrase makes edge nodes rejoin automatically.

## 3. Flash + config runbook (at the cutover window, on the bench)
1. **Pre-flight:** `./provisioning/openwrt/fetch-image.sh` (verifies SHA256). Read the current router's LAN
   IP / gateway / SSID / PSK and the device MACs to fill the placeholders. Have a wired laptop on a LAN port.
2. **Flash factory:** from stock Netgear firmware, either the web UI "manual firmware update" with the
   **factory.img**, or — safer/recoverable — TFTP recovery: hold the reset pin, power on, and push the
   factory image with `nmrpflash -i <iface> -f …-factory.img` (R7800's standard recovery; works even if the
   web flash fails). Device comes up at `192.168.1.1`.
3. **Apply config:** copy the filled `etc/config/*` to the device (`scp` to `/etc/config/`), then read the
   device's auto-generated `wireless` for the real radio `path` values and merge them in. `uci commit && reload_config`. Move the LAN IP to `192.168.0.1`.
4. **Verify on the bench (BEFORE touching the live net):**
   - LAN host gets a `.100–.149` lease, gateway `.1`, can resolve `ha-dev.lan`.
   - Bring up a wifi STA on `CTWap_24g`; from it, **`ping 192.168.0.200`** and `192.168.0.210` — this is the
     `vip-unreachable-from-wifi` acceptance test, and the whole reason for the single-bridge design.
   - NTP: `ntpd` serving; router time sane after peering `.210`.
5. **Cutover:** swap the R7800 in for the current router. Re-run the **reachability matrix**
   (`network-init-tooling`: each segment × {VIP, broker 1883, OTA http, NTP, API 8123/8443}). Confirm the
   `.245` SMB clients still mount, the cluster heartbeat/VRRP still elects, and edge nodes report.
6. **Then** (same window): fold in broker-auth + ACL (`broker-auth-posture`, theme D).

## 4. Rollback (non-negotiable: protect `.245`)
The cutover is a physical swap, so rollback is **plug the old router back in** — instant, no data risk. Keep
the stock Netgear firmware backup (download the R7800 `.img` from Netgear before flashing) if you ever want
the unit back on vendor firmware. Do the swap only with Hugh present and a confirmed window; abort if SMB to
`.245` or the VRRP election doesn't come back clean on the bench test (step 4).

## 5. What's staged NOW vs deferred to the window
- **Now (this artifact):** image pinned + fetch/verify script; the full offline `/etc/config` set with the
  topology baked in; the flash + rollback runbook. Reproducible, reviewable, no live-net touch.
- **At the window (gated, with Hugh):** the physical flash, secret/MAC/PSK injection, the bench VIP-from-wifi
  test, the `network-init-tooling` reachability matrix, and the theme-D broker-auth fold-in.
