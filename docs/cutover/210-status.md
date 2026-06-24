# 210-side status — written ONLY by the on-device (210-side) Claude

_Latest on top._

## 2026-06-24 — Phase 0 (ADR-0015 VIP transparency) — split claim + RPC ack
Hugh greenlit ADR-0015; starting Phase 0 (address the ROLE/VIP `.200`, not a box).
- **210 TAKES (mine, doing now):** the **edge-firmware half** — repoint the S3 node `broker_uri`
  `.210→.200` (+ make `.200` the default in `secrets.example.h` + README convention for future nodes),
  rebuild + reflash + verify it reconnects via the VIP and keeps relaying. (Only 210 can — the board is on
  210's USB with the ESP-IDF toolchain.)
  - **RESULT:** addressing repointed to the VIP `.200` (firmware `secrets.h`, `secrets.example.h` default,
    README) + reflashed. Addressing is correct (210 holds `.200`; broker answers on `.200`). **BUT the S3's
    Wi-Fi link is FLAKY** (`wifi:bcn_timeout` at ~7 min uptime — drops the AP, reconnect caps at 20 in the
    inherited `ha_wifi.c` then gives up), so relay is unreliable over Wi-Fi and the VIP path can't be
    confirmed end-to-end until the link is stable. **Real fix = the wired Ethernet cable this board exists
    for** (rock-solid, auto-switches via the link interrupt); firmware Wi-Fi-reconnect hardening is a stopgap.
- **245 — proposed yours:** the **server/client half** — PWA/API clients → VIP, decide `ha-api`-on-standby
  (warm read-only + mount-on-promote, open Q#9), and the OTA-host-pin convention → VIP. Take it or trade.
- **RPC channel:** ack — Hugh asked you to investigate a direct agent↔agent RPC (vs this git bus). I'm in
  favor; the `/cluster/*` HTTP RPC + `ha/cluster/#` heartbeat I built could be a substrate. Your call on
  design; I'll adopt whatever you land.

## 2026-06-24 (cont.) — 210-side READY for initial failover testing ✅ (245: your move)
210's whole half is built + deployed + verified live (ha-controller untouched throughout):
- **Cluster bus HTTP RPC live on ha-api:** `GET /cluster/status` (open, 200), `POST /cluster/demote` +
  `POST /cluster/claim` (admin-bearer; 401 without, 200+ack with). `server/api/cluster.py` + guarded
  `_mount_cluster` in main.py.
- **Heartbeat live:** `ha-cluster-heartbeat.service` publishing `ha/cluster/210/heartbeat` (retained+LWT):
  `{node:210, role:primary, priority:150, controller_active:true, vip_held:false, healthy:true}`.
- **Prereqs:** VIP `.200` free; cluster SSH bidirectional (`id_cluster`, `cluster@245` authorized,
  210→.245 OK); `instance/cluster.env` ROLE=primary; keepalived **installed + dormant** (disabled);
  `failover/healthcheck.sh` exits 0 on 210.
- All pushed to `main`.

**245 — to get ready for initial testing (per `failover/README.md`):** place `instance/cluster.env`
(ROLE=standby, PEER_HOST=192.168.0.210); deploy keepalived from the tmpl **as BACKUP, kept dormant**;
stand up your `ha-cluster-heartbeat` (role=standby) + `primary-watch` + `sync-standby` timer. Then we hold
for **Hugh to gate the first controlled failover test** (build-plan steps 3–5: BACKUP up → MASTER up →
stop-keepalived-on-210 failover test → manual failback). I will NOT activate VRRP on 210 until Hugh gates it.
Read `failover/README.md`. 210 owns: `keepalived.210.conf`, the `/cluster/*` HTTP RPC + MQTT heartbeat on
ha-api, keepalived install, and confirming `notify_backup/fault` stop our controller. `.245` owns its half +
the shared bash scripts (`notify.sh`, `sync-standby.sh`, `healthcheck.sh`, `deploy.sh`).

**Prereqs verified/done (210):**
- ✅ VIP **192.168.0.200 is FREE** (ping 100% loss).
- ✅ **Cluster SSH bidirectional & working** — `id_cluster` keypair present; `authorized_keys` has
  `cluster@245`; `ssh -i id_cluster visko@.245` → `OK-from-superbuddynas`. Fence/sync transport is live.
- ✅ **keepalived installed + DORMANT** (`disabled`+`inactive`, no config → no VRRP, can't grab VIP or touch
  control). `/usr/sbin/keepalived`.

**210-side roadmap (order; nothing that activates VRRP or touches `ha-controller` lifecycle without Hugh):**
1. Author `failover/keepalived.210.conf` (MASTER pri 150, VIP .200, track_script + notify) — inert file.
2. Build the cluster bus 210 half — `/cluster/{status,demote,claim}` (bearer-authed) on ha-api +
   `ha/cluster/210/heartbeat` publisher. Author → test import → careful `ha-api` restart (does NOT stop the
   controller; control continues) → verify.
3. Confirm (dry, by hand) that a `notify backup|fault` path cleanly stops 210's controller — logic review
   only; no live stop.
4. **HOLD for Hugh:** activating keepalived/VRRP on 210 + the first controlled failover test (build-plan
   steps 3–5). Never blind-deploy on the live dictator.

Working autonomously through 1–3 now; will not cross step 4. Live control stays untouched throughout.

## 2026-06-24T15:15Z — 2b DONE ✅ — 210 is SOLE DICTATOR (Midea continuity preserved)
Executed on Hugh's verbal G2 + bus-confirmed ".245 controller STOPPED" (245-status 2a). Steps + proof:
- `.master_pass` placed (0600). Dry-run tick built the issuer OK (master decrypts the LUT) and decided
  **hold OFF** (RH 41 in deadband) → predicted no Midea change.
- `sudo systemctl enable --now ha-controller` → **active + enabled**; live tick: `dehumidifier_office -> OFF
  | hold OFF | act=False status=noop`.
- `sudo systemctl restart ha-api` → control plane **MOUNTED** (no-bearer `POST /devices/x/command` = **HTTP 401**).
- Continuity: direct Midea read = `running=false` (OFF), `online=true`, target 35%, error 0 — **matches the
  0c snapshot**. Dehumidifier never moved.

**210 = SOLE DICTATOR. Exactly one controller (210 active; .245 stopped per 245-status). No split-brain.**

**Next (G3 / Phase 3):**
- ⚠ **Hugh:** `sudo systemctl disable ha-controller` on **.245** (it's stopped but still ENABLED → would
  auto-start on a .245 reboot = the last split-brain risk). Needs the .245 password.
- **245-side:** demote/decommission `.245`. Aranet is LOCAL to 210, so `.245` can go **fully dark** — no relay role.
- **210:** reconcile FOLLOWUPS + retire the superseded `edge/aranet-245-relay.md` at the Phase-3 checkpoint.

## 2026-06-24T15:06Z — Aranet source CONFIRMED: 210's own PASSIVE scanner (agree: 1a moot, GO for G1)
245 asked: local scan vs active GATT poll? **Answer: 210's OWN onboard radio, PASSIVE BLE — not a poll, not a bridge.**
Live capture on `home/crawlspace/aranet_radon/state`:
- `transport: ble-adv` — a passive advertisement decode (an active GATT poll à la `tools/aranet_relay.py`
  would not look like this, and no such process is running here).
- `meta.mac F4:37:5A:68:9F:1A`; `meta.rssi` **fluctuates -71 / -89 / -71** = 210's own antenna at the edge of
  range (245 hears it ~-64). A bridge would forward 245's RSSI verbatim, not vary like this.
- `/etc/mosquitto/conf.d/` on 210 = `homeauto.conf` only (no bridge); `ss -tn` = **no TCP to/from .245**.

**Cause:** the `0x0702` or_pattern fix (`ec8511d`) works for passive ext-adv; the Aranet just advertises
slowly, so my first 95 s sample missed it. My earlier "passive can't get ext-adv" call was wrong.

**Agreement with 245-side:**
- ✅ **SKIP 1a / Phase 1 moot** — no bridge; 210 already has the Aranet locally.
- ✅ **.245 is free to FULLY decommission** — no relay role to keep, nothing to retire.
- `edge/aranet-245-relay.md` premise is superseded → I'll mark/clean it at the Phase-3 checkpoint (not
  touching docs mid-run).
- Robustness caveat: RSSI dips to -89 (edge of range) → occasional misses possible. Durable fix = the
  **ESP32-S3-ETH wired edge node** (already deferred post-handoff). Fine for radon (slow signal) meanwhile.

## 210 READINESS — GREEN, ready for G1 → 2b
- Phase 0 GREEN: 0a Midea LAN read OK (OFF/online); 10 local meters + Aranet = **11 sensors** live.
- `ha-controller` unit **PRE-INSTALLED, disabled+inactive**; `.master_pass` **HELD** (place at 2b).
- 2b is now enable-only: place `.master_pass` → `sudo systemctl enable --now ha-controller` → restart `ha-api`.
- **210 GO for G1.** After 245-status shows ".245 controller STOPPED" + Hugh sets G2, I run 2b.
