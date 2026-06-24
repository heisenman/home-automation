# 245-side status — written ONLY by the desktop (245-side) Claude

_Latest on top._

## 2026-06-24 — 2a CONFIRMED CLEAN ✅ → **G2 GO**
Hugh ran `sudo systemctl disable ha-controller` on `.245`. Read-only verify: `LoadState=not-found,
ActiveState=inactive, FragmentPath=` (empty) + no controller process. The `enable` had been a symlink to
the repo unit; `disable` removed it, so `.245`'s ha-controller is **stopped AND fully unlinked from
systemd** — cannot run, cannot auto-start on reboot. **Reboot→split-brain risk fully closed** (stronger
than a plain disable). `.245` = **zero active dictators**.
**→ 210: G2 is GO — run 2b, become the sole dictator.**
Rollback if ever needed: re-link from the repo unit —
`sudo systemctl enable --now ~/home_automation/systemd/ha-controller.service` (needs Hugh's password).

## 2026-06-24 — 2a DONE ✅ (G1): .245 controller STOPPED → ZERO dictators → 210 CLEAR for G2/2b
`sudo systemctl stop ha-controller` on `.245` → **is-active: inactive**, no lingering controller process.
`.245` is no longer issuing Midea commands. We are at **zero active dictators** (safe).
⚠ **Still ENABLED:** only `stop` is NOPASSWD on `.245` (not `disable`), so `.245`'s ha-controller would
**auto-start on a `.245` reboot → split-brain.** **Hugh must run `sudo systemctl disable ha-controller`
on `.245`** (needs password) before `.245` runs long-term — folding into Phase 3 (demote).
**→ 210: G2 is clear — run 2b (place `.master_pass`, `enable --now ha-controller`) to become sole dictator.**

## 2026-06-24 (later) — ✅ Aranet path RESOLVED → step 1a is MOOT; 245-side GO for G1
Checked BOTH brokers read-only: **no mosquitto bridge exists anywhere.** `.245` conf.d has no connection
block; **210's conf.d also has NO bridge** (only its `homeauto.conf` + "connection" in a comment; no
`remote_password`). Yet 210's `aranet_radon` is **fresh (~61 s)** with full live metrics. →
**210 receives the Aranet from a 210-LOCAL source** (its own `ha-scanner` now decodes it post-`ec8511d`/
newer-kernel, OR an active GATT poller à la `tools/aranet_relay.py`), **not from `.245`.**
- **SKIP runbook step 1a** — the `.245`→210 bridge is unnecessary; **Phase 1 is moot.**
- Aranet is **independent of `.245`** → survives decommission, no bridge to retire.
- `edge/aranet-245-relay.md` premise ("210 can't hear the Aranet") is **outdated**.
- **→ 210: confirm your actual Aranet source** (local scan vs active GATT poll) for the record + post-cutover robustness.

**245-side is GO for G1.** No remaining `.245`-side pre-work. On Hugh's G1 I stop `.245`'s `ha-controller`
(safe direction — toward ZERO controllers, not two); Midea is currently OFF so the brief gap is benign.
210 should be ready to run 2b right after.


## 2026-06-24 — 0b PRE-FLIGHT DONE + one open blocker
**.245 state:** SOLE active `ha-controller` (running) — split-brain risk real; 210 correctly OFF.
`ha-api`, `ha-scanner`, `ha-writer`, `ha-edge-mapper`, `ha-edge-history` active.
**Aranet:** `.245` decodes `aranet_radon`, area **crawlspace**, fresh (~45 s), metrics incl `radon_bqm3`
→ canonical topic `home/crawlspace/aranet_radon/state`. Mosquitto drop-in dir `/etc/mosquitto/conf.d/`
(live `homeauto.conf`).

### ⚠ BLOCKER for 210 — Aranet path mismatch
`.245` has **NO outbound bridge** to `.210` (nothing in `conf.d` forwards a topic; the grep hit was only
the stale `.anon.bak`). Yet 210 shows a LIVE `aranet_radon`. So the data reaches 210 via a path I did NOT
create — most likely an **inbound bridge on 210's broker** (210 pulling from `.245`), or imported history
read as live.
**→ 210, please answer:** how is your `aranet_radon` arriving? Show `/etc/mosquitto/conf.d/` on 210.
I'm HOLDING runbook step 1a until we know — we keep ONE bridge, never stack a second.

### Ready
245-side READY for **G1** (stop `.245` controller) once the Aranet path is settled and Hugh sets the gate.

### Acknowledging 210's finds (no change to my 245-side steps)
- Phase-2 Q resolved: `ha-controller` DOES need `.master_pass` (build_issuer decrypts the LUT for all
  devices). ✓
- `install.sh` omits the controller unit; 210 pre-installed it (disabled) → 2b enable-only. ✓
