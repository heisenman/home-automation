# 245-side status — written ONLY by the desktop (245-side) Claude

_Latest on top._

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
