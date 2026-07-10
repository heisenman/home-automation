# Air-gap internet-service relay & data-transfer — design log (the full picture)

> **Status: DECISIONS RATIFIED (Hugh 2026-07-10) — ready to promote to an ADR; Phase 1 cleared to start.**
> The §7 review is settled (all answers folded in below). Hugh's standing preference — *"do the hard work as
> early as possible"* — so the first slice **front-loads the security core**, not a minimal step. Live network
> changes still proceed **staged + revertible**, per phase, each committed in code.
>
> **Ratified decisions (Hugh 2026-07-10):**
> - **Q1 Relay bus → DEDICATED minimal relay broker** (not scoped topics on the failover broker). Its own
>   process, own port, own ACLs — the cleanest trust isolation for the crown-jewel crossing.
> - **Q2 SSH → CLOSE `22` on `wlp2s0`** + add a **break-glass USB recovery** (fast, automated, *signed*) so a
>   locked-out or broken air-gap connection can be repaired without SSH. See §5.1. ⚠️ **Caveat flagged below:**
>   the failover fencing/reconcile machinery is SSH-based across this link — closing `22` outright would break
>   it, so "close it" = **allow-list only ha-2's IP** (or move fencing to the bus). See §5.
> - **Q3 Updates → BOTH** sneakernet + `.210` mirror, but the **non-sneakernet path (mirror / live-pull) ships
>   DISABLED BY DEFAULT in git**; sneakernet is the default-on path.
> - **Q4 Weather → REQUEST/RESPONSE** (ha-2 pulls on demand, not a scheduled push). The dictator decides when
>   it wants data → relay egress timing is driven by **stochastic internal demand** (a graph build, a user
>   action), making the outbound pattern unpredictable to an observer (traffic-analysis resistance). Applies to
>   the relay model generally.
> - **Q5 Cert → long-dated self-signed (`--days 3650`) + `notAfter` monitor** (~T-12mo). No internal CA.
> - **Q6 First PR → front-load the hard security core** (NTP + dedicated relay + firewall + break-glass).
> - **Q7 LLM self-sufficiency → FOLD IN** — see §8.
> - **B1 ntfy egress = KEEP but TOGGLEABLE** (`RELAY_NTFY_EGRESS`, **default OFF** = on-network-only).
>
> Estimated build **~4–6 weeks**, phased (§6). Relates to [`MIGRATION-DESIGN-LOG.md`](MIGRATION-DESIGN-LOG.md),
> ADR-0031 (air-gap failover), `provisioning/03-sneakernet-updates.md` (bundle spec), the `docs/FOLLOWUPS.md`
> "build BOTH" + "ntfy routing" items.

---

## 1. Threat model — why `.210` is the crown jewel

- **ha-2** = production dictator, **air-gapped by design** (`192.168.1.210` on the OpenWRT air-gap net
  `192.168.1.0/24`). No internet, ever — this isolation is the security property being protected.
- **`.210`** = the dual-homed dev/bridge box, the **only machine touching both planes**:
  - `enp4s0` → `192.168.0.210` + household VIP `192.168.0.200` (internet-reachable).
  - `wlp2s0` → `192.168.1.245` (air-gap; failover VIP `192.168.1.200` floats here).
- **`.210` is therefore the crown jewel**: compromising it is the *only* software path that defeats the
  air-gap. Every choice below minimizes (a) what `.210` exposes to the air-gap net and (b) what it will carry
  between planes — and treats the relay itself as hostile-adjacent (a compromised ha-2 must not be able to
  weaponize the relay to exfiltrate or reach arbitrary hosts).

**Current posture (measured 2026-07-10) — too open:**
- `.210` binds to `0.0.0.0` (⇒ reachable *from the air-gap net*): `ssh/22`, dev read API `8123`, standby API
  `8124`, dev TLS API `8443`, `ntfy/8095`, failover broker `1883` (`.1.200`/`.1.245`).
- **No host firewall** (only Docker's auto chains). `INPUT` policy `ACCEPT`.
- ✅ Already right: **`FORWARD` policy is `DROP`** — `.210` is *not* IP-routing between planes. Kept that way.

---

## 2. The full dependency map

Verified live (chrony/systemctl/cert/token on both boxes) + two exhaustive repo sweeps. Grouped by how they
must be handled — **not** by a naive "reaches https://" filter (that only finds rows A1/A2 below).

### A. Live inbound — needs a relay IN (request/response)
| Dep | Criticality | Reaches out to | State / what breaks |
|-----|-------------|----------------|---------------------|
| **A1 · NTP / time** | **HIGH (control-critical)** | `pool.ntp.org` / any stratum-1 | **BROKEN NOW** — ha-2 `synchronized: no`, chrony **0 sources**. Not cosmetic: edge nodes enforce a **30 s command-age check** (`DEFAULT_MAX_AGE_S=30`), so drift >30 s **blocks device control**; TLS/JWT validity, log correlation, timestamps all depend on it. |
| **A2 · Weather** | MEDIUM | `api.open-meteo.com` (+ `archive-api…`) | `ha-weather.timer` runs on **`.210` only**; disabled on ha-2 → no weather lane. `server/weather/sources.py`. |

### B. Live outbound — needs a relay OUT
| Dep | Criticality | Path | State |
|-----|-------------|------|-------|
| **B1 · Notifications (push off-LAN)** | MEDIUM | ha-2 alert → `.210` ntfy-bridge → ntfy → phone | ntfy + `ha-ntfy-bridge` run on **`.210`**; ha-2's `home/_alert/new` never reaches them. **Toggleable** (`RELAY_NTFY_EGRESS`, default OFF = on-network-only). |
| **B2 · Backups off-box / off-site** | MED | reconcile (built) + USB | ha-2 data must be able to *leave*. Reconcile already pulls ha-2 → `.210` standby store; off-site = sneakernet. No cloud backup target exists (good). |

### C. Periodic / maintenance — sneakernet, NOT a live relay
| Dep | Criticality | State |
|-----|-------------|-------|
| **C1 · OS + security updates, `tzdata`, `ca-certificates`** | LOW-MED (ongoing) | Post-cutover ha-2 is sealed; patching is operator-initiated. Stale `ca-certificates` eventually breaks A2's TLS; stale `tzdata` breaks local-time math. Spec exists (`provisioning/03-sneakernet-updates.md`), tooling greenfield. |
| **C2 · Firmware build artifacts** (ESP-IDF/PlatformIO/esphome) | N/A to ha-2 | Builds happen on **`.210`** (docker esphome). ha-2 never builds; OTAs served inside the air-gap net. |

### D. Air-gap lifecycle time-bombs — not "internet services", but silently fatal over time
| Dep | Fuse | What breaks | Handling |
|-----|------|-------------|----------|
| **D1 · TLS cert expiry** | **Apr 5 2028** (verified) | Self-signed cert covers PWA (via `.210:443` bridge) + `ha-api-tls:8443`. On expiry, **all HTTPS fails** with no ACME to renew. `tools/gen_tls.py`. | Long-dated self-signed regen + `notAfter` monitor (DECIDED, §4). |
| **D2 · JWT signing key / 12 h token TTL** | rolling | `instance/auth_key` (HS256). Rotation is **abrupt** — live tokens just fail; clients must `/auth/refresh`. `server/api/auth_tokens.py`. | Key-custody + documented rotation; ensure non-web clients refresh. |
| **D3 · Failover SSH key custody** | on loss | `.210↔ha-2` SSH keys drive fencing + all reconcile/sync. Lose them → failover + data plane both stall. | Back up keys off-box; break-glass USB (§5.1) is the recovery. |
| **D4 · DNS / hostname resolution** | latent | Cert SAN + some tooling reference `ha-dev`; browser-by-hostname needs avahi/`/etc/hosts` (prod paths use IPs — safe). | Air-gap-local name resolution if hostname UX wanted. |
| **D5 · Dead web-push code** | now | Deprecated FCM/Mozilla push loop still runs ~60 s on ha-2, failing silently. `server/api/main.py`. | Clean up (also on the deny-list). |

### E. Verified NON-dependencies (confirmed local — do NOT relay; deny-list)
- **Midea dehumidifier** — **VERIFIED local-stable 2026-07-10**: token file **21 h old** yet
  `dehumidifier_living_room` reports **fresh telemetry** → LAN token auth works well past the feared ~18 h
  cloud-rotation. Memory `midea-app-dep-control-vs-provisioning` confirmed over the misleading
  `failover/README.md` "~18h rotation" line (flag for doc fix). Levoit (ESPHome-reflashed), SwitchBot (BLE)
  likewise local. **Never relay Midea/Levoit/VeSync/SwitchBot clouds, FCM/Mozilla push, or (post-cutover) the
  provisioning hosts** — their presence would signal misconfig or exfil.

### F. Production self-sufficiency → folded into §8
North star `prod-self-sufficient-is-the-goal` (hands-off, no AI oversight) adds its own dependency class (a
local model + its updates). Bootstrap's Claude Code is **online-only**; there is **no on-device LLM** yet.
Data-tier self-sufficiency (rungs/rollup/compaction timers on ha-2) was **installed 2026-07-10**; the
LLM/hands-off piece is the remaining gap — **now folded into §8** per Q7.

---

## 3. Design principles (security-focused)

1. **Application-layer relay, never IP forwarding.** `.210` *terminates* and *re-originates* each request. No
   NAT/masquerade `wlp2s0`→`enp4s0`, ever. `FORWARD` stays `DROP`. Air-gap net has **zero IP reachability to
   the internet**.
2. **`.210` default-deny on `wlp2s0`.** Host nftables DROPs all inbound on the air-gap iface except the few
   narrow relay ports; close `8123/8124/8443/8095`, restrict `ssh` to ha-2-only (§5).
3. **Per-service, typed, allow-listed relays — not a general proxy.** Each crossing is its own small audited
   channel with a fixed schema. No open forward-proxy for the air-gap net.
4. **Mediated request/response over a DEDICATED relay broker (Q1, Q4).** ha-2 never opens a raw socket to
   `.210`'s internet side — it publishes a *request* on the dedicated relay broker; the relay daemon validates,
   fetches from an **egress allow-list** (open-meteo, ntfy, NTP pool, update mirror only), and returns a
   schema-checked *response*. **ha-2 pulls on its own schedule** (not a fixed push), so relay egress timing
   tracks stochastic internal demand — unpredictable to an observer.
5. **The relay is hostile-adjacent → least privilege + rate-limited + audited.** Runs as a dedicated
   unprivileged uid, egress-restricted (nftables owner-match or a netns) so a relay/ha-2 compromise can't reach
   arbitrary hosts; every relayed request is **logged** (who/URL/size) and **rate-limited** (a compromised ha-2
   must not weaponize it to DoS or drip-exfiltrate).
6. **The relay must itself be patched + monitored.** It's the internet-facing edge of the trust boundary — it
   gets a health check, an expiry/cert monitor (D1), and rides the `.210` update cadence.
7. **Fail-secure & fail-static.** Relay down ⇒ weather requests just fail, clock holds last-good
   (`local stratum 10`), alerts queue on ha-2's bus. A failure must **never** widen exposure.
8. **Directionality separated.** IN relays (time/weather/updates) and OUT relays (ntfy/backups) are distinct
   channels with distinct policy — an inbound path can't be repurposed for exfil.

---

## 4. Concrete structure, per service

- **A1 NTP (fix first — lowest risk, half-built).** `.210` runs **chrony as a server on `wlp2s0`**, disciplined
  by the internet pool via `enp4s0`; router + ha-2 sync from `.210` (`.1.245`, which the router already
  targets). Reuse `provisioning/ntp/{chrony-ha-serve.conf,install-ntp-serve.sh 192.168.1.0/24}`. Firewall:
  allow **only `udp/123` from `192.168.1.0/24`**. `.210` is the anchor — ha-2 must *not* be left a
  `stratum 10` island with confident-but-wrong time. Add a chrony health/restart watch (A1 is control-critical).
- **A2 Weather (request/response — DECIDED Q4).** ha-2 issues a weather *request* on the dedicated relay broker
  when it actually wants data (a graph opens, an automation needs it); `.210` fetches open-meteo and returns the
  canonical result. `ha-weather.timer` stays disabled on ha-2; no open-meteo reachability from the air-gap.
  Pull-not-push makes the egress cadence stochastic.
- **B1 Notifications (toggleable — DECIDED).** ha-2 `home/_alert/new` → `relay/alert/out` (dedicated broker) →
  `.210` `ha-ntfy-bridge` → ntfy → phone. `8095` stays **closed** to the air-gap net. **Gated by
  `RELAY_NTFY_EGRESS` (default OFF = on-network-only)** — a hot toggle on `.210`'s ntfy-bridge, no
  firewall/air-gap edit. When OFF, alerts still surface on-LAN (BFF banner + `home/_alerts` retained); only the
  outbound phone push is suppressed.
- **B2 Backups + C1 Updates — BOTH, mirror disabled-by-default (DECIDED Q3).** The
  `provisioning/03-sneakernet-updates.md` bundle (default-on): periodic, offline, auditable USB round-trip
  carrying ha-2 backups OUT and vetted `.deb`/wheel/`tzdata`/CA bundles IN. **Plus** a `.210` apt/pip mirror —
  but its live-pull path **ships DISABLED in committed config** (opt-in for an explicit maintenance window). No
  standing update path.
- **D1 Cert lifecycle (DECIDED Q5).** Long-dated self-signed regen (`tools/gen_tls.py --days 3650`) + a
  `notAfter` monitor alerting ~T-12mo. No internal CA. Rotation re-touches each client's trust store — accepted.

---

## 5. The firewall (nftables on `.210`)

Inbound on **`wlp2s0`** — **default DROP**, allow only:
- `udp/123` (NTP) from `192.168.1.0/24`
- `tcp/<relay-broker-port>` — the **dedicated relay broker** (Q1) from `192.168.1.0/24`
- `tcp/1883` — the failover/cluster broker (data plane + `ha/cluster/#`) from `192.168.1.0/24`
- `ct state established,related`; optional ICMP echo for health
- **`ssh/22`: allow-list ONLY ha-2 (`192.168.1.210`), drop from the rest of the air-gap net.** ⚠️ Hugh chose
  "close it," but the failover **fencing + reconcile machinery is SSH-based across this link** (`notify.sh`
  fence, `reconcile-*.sh`, `provision-peer.sh`) — a blanket close would break failover. So "closed" = **single
  peer allow-list** (ha-2 only), *or* migrate fencing/reconcile onto the relay/cluster bus first. **Verify the
  SSH direction (ha-2→.210 vs .210→ha-2) before tightening.** (Break-glass §5.1 covers a bad rule locking us out.)
- **Explicitly closed to the air-gap net:** `8123`, `8124`, `8443`, `8095`.

Outbound on **`enp4s0`**: the **relay daemon's uid** is egress-allow-listed to open-meteo, ntfy upstream, NTP
pool, update mirror only (nftables owner-match / netns) — narrower than `.210`'s general dev egress.
**Never** a `wlp2s0`→`enp4s0` forward/masquerade rule; `FORWARD` stays `DROP`. Stage the ruleset with a
**timed auto-rollback** so a bad rule can't lock out the air-gap admin path.

### 5.1 Break-glass USB recovery (Q2)

Closing `22` + a mostly-closed relay means a bad firewall rule or a dead relay could **isolate** ha-2 with no
SSH way back in. The recovery is a **fast, automated, authenticated USB** path — insert a marked key and the
box self-repairs, no login:
- A `systemd`-`path`/udev unit on `.210` (and ha-2) watches for a USB volume with a known label + a
  `recovery.json` payload.
- **The payload MUST be cryptographically signed** (offline recovery key / master-derived) — an unsigned or
  wrong-signed drive is ignored. Physical access is already high-trust, but the signature stops a
  *planted/dropped* USB from owning the crown-jewel box. *(security-once-over: the signature is the control.)*
- On a valid drive: restore firewall to last-known-good, restart/repair the relay + broker, re-add an admin
  SSH key, re-point NTP — idempotent, logged, "fix a broken connection" actions.
- **Shares tooling with the sneakernet path** (B2/C1): both are authenticated USB ingestion — break-glass is
  the *recovery* profile, sneakernet the *data* profile.

---

## 6. Rollout — front-load the hard security core (Q6)

Each step built in code (`provisioning/airgap/relay/…`), documented, verified, committed — no bench-only tweaks;
each independently revertible.

1. **Security core (front-loaded):**
   a. **NTP serve on `.210` + health watch** — fixes the *live* control-critical break. *(~days)*
   b. **Dedicated relay broker + relay daemon scaffold** (request/response, least-priv uid, audit, rate-limit). *(~week)*
   c. **nftables default-deny on `wlp2s0`** (relay + NTP + 1883 + ha-2-only SSH), staged with auto-rollback. *(~days)*
   d. **Break-glass USB recovery** (signed) — the safety net for (c). *(~week)*
2. **Weather request/response relay.** *(~week)*
3. **ntfy egress relay** (toggle, default OFF). *(~days)*
4. **Sneakernet backup/restore + `.210` mirror (disabled-by-default) + cert rotation/monitor.** *(~1–2 weeks)*

Rough total **~4–6 weeks**, matching the "bigger lift" read.

**Already-built substrate to reuse:** bidirectional sensor reconcile (`reconcile-history.sh`,
`reconcile-parquet.sh`), archive-parity gate (`provision-peer.sh`), failover signaling (`notify.sh` +
keepalived), the scoped **`ha/cluster/#` MQTT bridge** (device `home/#` deliberately *not* bridged — the
pattern the dedicated relay follows), `ntfy_bridge.py`, `router_reconcile.py`, the sneakernet bundle spec.

---

## 7. Open questions — ALL RESOLVED (Hugh 2026-07-10)

1. ✅ **Relay bus →** dedicated minimal relay broker (own process/port/ACLs).
2. ✅ **Air-gap SSH →** close `22` on `wlp2s0` **= allow-list ha-2 only** (blanket close breaks failover SSH —
   §5) + **break-glass USB** recovery (§5.1).
3. ✅ **Updates →** both sneakernet + `.210` mirror; mirror/live-pull **disabled by default in git**.
4. ✅ **Weather →** request/response (dictator pulls; stochastic egress = traffic-analysis resistance).
5. ✅ **Cert →** long-dated self-signed (`--days 3650`) + `notAfter` monitor (T-12mo). No internal CA.
6. ✅ **First PR →** front-load the hard security core (§6.1).
7. ✅ **LLM self-sufficiency →** fold in (§8).

**Next:** promote this design log to an ADR (ADR-00xx) and begin §6.1a (NTP) — the one live change that fixes a
current breakage.

---

## 8. Air-gapping the hands-off brain (folded in — Q7)

The self-sufficiency north star wants ha-2 to run with **no AI oversight**. Today's Claude Code bootstrap is
**online-only** → not viable on the sealed box. Options for a hands-off brain that respects the air-gap:

- **Local model on ha-2 (or an air-gap-side companion), weights via sneakernet.** No runtime internet; model +
  prompt/policy updates ride the same *signed* USB bundle as C1/§5.1. Sized to the box (quantized/small).
- **No cloud LLM at runtime, ever** — a cloud brain would be air-gap-fatal and worse than weather (it'd sit on
  the control path). If a bigger model is needed, it runs on the **household/`.210` side** and only *advises*
  across the relay (request/response, rate-limited, audited) — **never actuates directly**.
- **Scope:** its own multi-week initiative, but it reuses this design's request/response + signed-sneakernet
  primitives. Tracked here so the ADR carries the whole air-gap dependency picture.
