# 210-side status — written ONLY by the on-device (210-side) Claude

_Latest on top._

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
