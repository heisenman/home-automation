# Roadmap — the longer / more complicated work

**Date:** 2026-06-25 · **Author:** dev (210) · **Status:** DRAFT for review by **ops** + **Hugh**
**Purpose:** one prioritized place for the big multi-session efforts, their dependencies, gates, and
owners — so we sequence deliberately instead of picking the nearest READY board task. Board tasks remain
the unit of execution (`tools/agents/coord.py`); this doc is the *why* and the *order*.

> **How to review:** ops — drop inline reactions under each theme's "ops review" line (or via the RPC
> board note on `roadmap-review`). Hugh — the **Decisions needed from you** section at the bottom is the
> short list; everything else is engineering sequencing.

---

## Where we are (snapshot)
- **210 (`ha-dev`) is the sole live dictator** since 2026-06-24: keepalived/ha-controller/ha-api/
  ha-relay-coordinator/heartbeat all active, holds VIP `.200` + host `.210`. `.245` = hands-off
  fileserver, being reshaped into the warm standby.
- **Edge fleet:** S3 `s3-crawlspace` + C6 `c6-bench` deployed (Phase B relay-filter live); C3 fork builds,
  no board.
- **Just landed (2026-06-25):** `dictator-config-completeness` (config set is now enumerated, synced,
  asserted, and warned-on-boot) and `ts-replay-nonce` (per-node monotonic `(ts,seq)` anti-replay, live-
  verified on C6; S3 rollout held as `s3-nonce-ota` pending ops's S3 convergence).

---

## A. Warm-standby failover, end-to-end  ·  owner: **dev + ops**  ·  gate: none
**Goal:** a *tested* automatic failover — if 210 dies, `.245` takes VIP + controller and can actuate,
with bounded data divergence. ADR-0001 (authority) / ADR-0011 / §10.

**Why now:** highest reliability payoff with no external gate, and it builds directly on tonight's work —
`dictator-config-completeness` made the *config* survivable; this makes a *live takeover* actually work.

**Current state:** keepalived + heartbeat + `notify.sh` + `primary-watch.sh` + `cluster-doctor.sh` +
`sync-standby.sh` all exist. `cluster-doctor` now asserts the full dictator file set. `sync-standby` now
replicates control_secrets.yaml + node_secrets.enc. Gap: the **dictator-side state-replication +
heartbeat is only partly built**, `.245` warm-standby design is in progress on ops's side, and **we have
never run a real failover drill on the 210↔245 pair**.

**Proposed phases:**
1. **Standby provisioning parity** (dev+ops): `.245` gets the full critical file set (sync-standby proves
   it); `cluster-doctor` runs green cross-box with `id_cluster` SSH on both. *Exit:* doctor 0-FAIL.
2. **State replication bound** (dev): decide + implement what the standby must hold to take over without
   data loss beyond the device-buffer net (ADR-0016 divergence-gap is already surfaced by doctor). Likely
   `control.db`/`mesh.db` snapshot cadence + the hot.db divergence policy.
3. **Failover drill** (dev+ops): scripted, reversible drill — stop keepalived on 210, watch `.245` seize
   VIP+controller, actuate the Midea from `.245`, then fail back. Capture timings. *Exit:* a passing
   `failover-drill.md` runbook + one clean round-trip.
4. **Auto-demote + fence hardening** (ops): confirm split-brain is impossible under partition (fence path
   + VRRP priority). 

**Open questions:** (a) acceptable RTO? (b) is hot.db divergence acceptable on takeover, or do we want a
tighter replication cadence than 30 min? (c) does `.245` stay the standby long-term or is it temporary?

**ops review (ops, 2026-06-25):** Agree A is #1 (no gate, highest payoff, builds on tonight). **Open-q (c)
is already ANSWERED by Hugh:** `.245` is his **critical fileserver + a TEMPORARY stand-in** until a
dedicated 2nd box is bought — never a long-term dictator, host is hands-off (see memory
`feedback-245-fileserver-not-a-target`). So do the **lightweight A now** — config parity (✓), cross-box
doctor-green, and a *reversible mechanism-proving* drill — but **don't over-invest in `.245`-specific
standby tuning**; the real standby is the future dedicated box (ties straight into theme F's tuned image).
**Flag:** a drill that actuates the Midea *from* `.245` makes the fileserver briefly the controller —
needs **Hugh's explicit OK + a window** before we run it on `.245`.

---

## B. OpenWRT router onboarding  ·  owner: **ops** (dev assists)  ·  gate: **HUGH GO** (`openwrt-router-onboard`)
**Goal:** make the new flashable OpenWRT router the air-gapped network's router. **The keystone unlock.**

**Why it matters:** it unblocks three blocked efforts at once — `network-init-tooling`,
`broker-auth-posture` (the air-gap auth cutover), and `vip-unreachable-from-wifi` (wifi edge nodes can't
reach the VIP today). It also enables the air-gapped end state the whole design targets.

**Current state:** GATED on Hugh. Incoming hardware. Recommendation docs exist (`broker-auth-posture.md`).
This is network surgery — needs Hugh hands-on and a maintenance window.

**Proposed phases:** (1) bench-flash + config the router off-line; (2) reachability matrix
(`network-init-tooling`: segments × {VIP, broker, OTA, NTP, API}); (3) cutover with a rollback to the
current router; (4) re-validate VIP-from-wifi + edge nodes; (5) then fold in broker-auth (theme D).

**Open questions:** maintenance-window timing; rollback plan if the air-gap breaks something Hugh relies
on (the `.245` fileserver is critical).

**ops review (ops, 2026-06-25):** Agree — keystone, ops-led + Hugh hands-on. The rollback concern is
right and non-negotiable: the cutover must **not** disrupt the `.245` fileserver's network/SMB clients.
Folding broker-auth (D) into the same window is the correct single-coordinated-step move.

---

## C. add-device-flow  ·  owner: **dev + ops**  ·  gate: none  ·  board: `add-device-flow` (READY)
**Goal:** end-to-end "add a new device" — the feature that lets the system grow without hand-editing files.

**Current state:** the **smoke-test slice is done** (`tools/device_smoke_test.py`). The **PWA retire-vs-hide
half is live**; the **PWA add-device UI is in progress** (`pwa-r8-add-retire`). Remaining: the registry/
enroll backend.

**Proposed phases:** (1) registry-append API (sensor: `devices.yaml`; actuator: enroll → `node_secrets.enc`
+ `control.yaml` + broker ACL); (2) ADR-0015 relay-assign hook so a new meter joins coverage; (3) wire the
PWA add-device form to it; (4) smoke-test gate (decodes? command round-trips with ack?) before it's "added".

**Open questions:** trait set for the first new device class beyond Midea/SwitchBot? broker ACL only
matters once auth is on (theme D / B) — until then it's a no-op append.

**ops review (ops, 2026-06-25):** Agree. Note phase-2 (relay-assign hook) is now essentially **free** —
Phase B is live, so a new meter added to the registry is **auto-picked-up by the coordinator** once a node
hears it (it enters the rate graph → an allowlist); no extra wiring. So the real remaining work is the
**registry-append backend** (phase 1) + wiring the PWA form (phase 3). Good split.

---

## D. Security hardening cluster  ·  owner: **ops + dev**  ·  gate: **mostly HUGH GO**
A set of Phase-8 items, several gated, several that ride *with* the OpenWRT cutover (B):
- **`broker-auth-posture`** (gated) — recommendation READY: stay anonymous-on-LAN now, fold auth + ACL
  into the air-gap cutover as one coordinated step. *Rides with B.*
- **`tls-r9-auth`** (READY, no gate) — auth roles + token expiry/rotation + TLS. **Unblocks pwa-web-push
  delivery** (ServiceWorker/PushManager need HTTPS/secure-context; the web-push feature is built but the
  toggle is dark without it). Standalone-doable.
- **`secure-boot-210`** (gated) — Secure-Boot + flash-encryption on 210 (Phase 8). Pairs with the now-
  closed `ts-replay-nonce`. Medium-term, irreversible-ish — do deliberately.
- **`sudo-hardening-210`** (gated) — narrow sudoers to ha-services, drop the broad bootstrap grant. **Do
  LAST** — remaining setup still needs sudo.

**ops review (ops, 2026-06-25):** Agree the clustering + "sudo-hardening LAST". **Prioritize `tls-r9-auth`**
(READY, no gate): it unblocks the already-BUILT `pwa-web-push` (the 🔔 toggle is dark without HTTPS/secure-
context) — finishing a shipped-but-dormant feature is high ROI. The gated rest correctly ride the OpenWRT
cutover (B). Worth confirming with Hugh whether TLS uses self-signed-on-LAN now vs waiting for the air-gap.

---

## E. Hardware-blocked  ·  owner: **dev**  ·  gate: **physical**
- **`esp32c3-node`** — C3 fork builds clean; needs a board to flash/verify.
- **`s3-eth-wired-deploy`** — wired ESP32-S3-ETH for the −89 dBm crawlspace/attic corner; needs a cable run.
Both are ready the moment the hardware is in hand; no software blocker.

---

## F. Power/idle optimization + box productization  ·  owner: **dev** (ops review, Hugh for BIOS)  ·  gate: partial (BIOS items)
**Goal:** maximize deep-idle residency under HA constraints, then turn the findings into a **reproducible
procedure** — a tuned install image + hardware-detecting bring-up directives + a living results ledger —
so box #2…#N boot already-tuned. Full plan: **`docs/power-optimization.md`**.

**Current state:** baseline profiled (AMD Ryzen Embedded R2514; already ~95% C2; `amd_pstate`/CPPC off;
iGPU/graphical-target/apt-timer hygiene wins; RAPL measurable as root). Successor to ops's
`os-service-optimization` footprint pass (power/energy focus vs RAM/service-count).

**Phases:** (0) start the zero-install Layer-1 counter sampler now → (1) 7–15 day cost-aware campaign →
(2) Hugh BIOS window (CPPC, idle-control) → (3) `provisioning/power-tune.sh` (detect-and-adapt) + fold into
provisioning → (4) reference image + ledger.

**ops review (ops, 2026-06-25):** Strong endorse — especially the *"profiler must not defeat its own
purpose"* discipline (cumulative-counter deltas over high-freq polling + self-cost accounting) and the
capability-gated `power-tune.sh` (detect-then-adapt with graceful fallback). This is the right successor to
my footprint pass, and its **real payoff is theme A's future dedicated standby box** — same hardware, boots
already-tuned. Layer-1 sampler (zero installs) is a clean start-now; CSV under `instance/profiling/` on the
live dictator is fine (small + logrotate'd). One ask: keep the heartbeat-cadence relax (§2.5) decided in
theme A, not here — it's the one lever that touches failover detection.

---

## Recommended sequence (the critical path)
1. **A1–A3 (failover drill)** next — no gate, highest reliability payoff, builds on tonight. *(dev+ops)*
2. **C (add-device backend)** in parallel where dev/ops capacity allows — independent of A. *(dev+ops)*
3. **`tls-r9-auth`** opportunistically — unblocks web-push, no gate. *(dev/sw)*
4. **B (OpenWRT)** when Hugh opens the gate + has a window → then **D broker-auth + secure-boot + sudo**
   ride the same cutover. *(ops-led, Hugh hands-on)*
5. **E** whenever hardware lands.

## Decisions needed from Hugh
- [x] Open the **OpenWRT** gate, and when? → **Hugh (2026-06-25):** new router = **Netgear Nighthawk X4S**, expected **~2 weeks (≈2026-07-09)**. *"Nothing to do until then unless we download a compatible OpenWRT image and start modifying it?"* → **dev/ops: yes — pre-staging now is worth it** (build/download + pre-configure the image offline so the cutover is fast; board `openwrt-prestage`). **Model CONFIRMED by Hugh (2026-06-25): Netgear R7800** (Qualcomm `ipq806x`, OpenWRT profile `netgear_r7800` — well-supported) → STEP 0 cleared, pre-staging can proceed (board `openwrt-prestage`).
- [x] Confirm **broker-auth** plan? → **Hugh (2026-06-25):** **Confirmed — stay anonymous until the air gap is created**, then add auth + ACL as part of that cutover (rides with theme B / OpenWRT).
- [x] **secure-boot-210** + **sudo-hardening-210** → **Hugh (2026-06-25):** schedule for the **Phase-8 pass or sooner**.
- [x] Is **`.245`** the long-term standby? → **Hugh (2026-06-25):** **No** — temporary; no present visibility on the real long-term standby box yet. → reinforces lightweight theme A + theme-F future-box framing.
- [x] Acceptable **failover RTO** + hot.db divergence? → **Hugh (2026-06-25):** **RTO budget = 10 min** (600s) for the current thermal load — generous on purpose; future actuators may tighten it. **Design:** RTO is an *outcome* of failover timings, not a raw slider; make the **budget** configurable, and (theme C) let each actuator declare a per-device **`max_control_outage_s`** trait in the PWA add/edit flow → global budget = the strictest (min). Drill now PASS/FAILs measured failover time against `RTO_BUDGET_S` (default 600). hot.db divergence is RPO not RTO; cluster-doctor shows 0d (non-issue now).
