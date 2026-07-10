# Air-gap internet-service relay & data-transfer — design log (the full picture)

> **Status: DRAFT / running design log (2026-07-10) — IN HUGH REVIEW.** Destined for an ADR once ratified.
> **No live firewall/relay changes have been made, and none will be until Hugh's design review settles the §7
> open questions** (Hugh 2026-07-10: "design review first, no code yet — then we scope PR1"). This is the
> proposal to review.
>
> **Decisions so far (Hugh 2026-07-10):**
> - §7-Q5 cert = **long-dated self-signed (`--days 3650`) + a `notAfter` monitor alerting ~T-12mo** (not an
>   internal CA). Trade-off accepted: each rotation re-touches every client trust store, but the mechanism
>   stays dead-simple.
> - **B1 ntfy egress = KEEP, but TOGGLEABLE.** Off-network push is most valuable exactly when away, so the
>   relay stays — but it's gated by a runtime switch (`RELAY_NTFY_EGRESS`, **default OFF** = on-network-only,
>   matching Hugh's "only on-network devices get the annoying messages" preference). Flip on for off-network
>   push. Toggle is hot (no air-gap/firewall change to flip it).
>
> Scope note (Hugh, 2026-07-10): the *service count* is small, but the *lift is not* — the relay is the single
> most security-sensitive component in the system (the one box straddling the trust boundary), so "doing it
> properly" is a **hardened, bidirectional subsystem** with its own threat model, firewall, ACLs, patch story,
> monitoring, and failure modes — plus a set of **air-gap lifecycle time-bombs** (cert expiry, key custody)
> that aren't "internet services" but will silently break an isolated box over months. Estimated build:
> **~4–6 weeks**, phased. Relates to [`MIGRATION-DESIGN-LOG.md`](MIGRATION-DESIGN-LOG.md), ADR-0031 (air-gap
> failover), `provisioning/03-sneakernet-updates.md` (bundle spec), the `docs/FOLLOWUPS.md` "build BOTH" +
> "ntfy routing" items.

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
must be handled — **not** by a naive "reaches https://" filter (that only finds rows A2/A/… below).

### A. Live inbound — needs a continuous relay IN
| Dep | Criticality | Reaches out to | State / what breaks |
|-----|-------------|----------------|---------------------|
| **A1 · NTP / time** | **HIGH (control-critical)** | `pool.ntp.org` / any stratum-1 | **BROKEN NOW** — ha-2 `synchronized: no`, chrony **0 sources**. Not just cosmetic: edge nodes enforce a **30 s command-age check** (`DEFAULT_MAX_AGE_S=30`), so clock drift >30 s **blocks device control**, and TLS/JWT validity, log correlation, and data timestamps all depend on it. |
| **A2 · Weather** | MEDIUM | `api.open-meteo.com` (+ `archive-api…`) | `ha-weather.timer` runs on **`.210` only**; disabled on ha-2 → no weather lane. Fails silently to `None`, no local cache. `server/weather/sources.py`. |

### B. Live outbound — needs a continuous relay OUT
| Dep | Criticality | Path | State |
|-----|-------------|------|-------|
| **B1 · Notifications (push off-LAN)** | MEDIUM | ha-2 alert → `.210` ntfy-bridge → ntfy → phone | ntfy + `ha-ntfy-bridge` run on **`.210`**; ha-2's `home/_alert/new` never reaches them. On-LAN the phone can poll ntfy; off-LAN needs `.210`'s internet leg. **Toggleable** (`RELAY_NTFY_EGRESS`, default OFF = on-network-only). |
| **B2 · Backups off-box / off-site** | MED | reconcile (built) + USB | ha-2 data must be able to *leave*. Reconcile already pulls ha-2 → `.210` standby store; off-site = sneakernet. No cloud backup target exists (good). |

### C. Periodic / maintenance — sneakernet, NOT a live relay
| Dep | Criticality | State |
|-----|-------------|-------|
| **C1 · OS + security updates, `tzdata`, `ca-certificates`** | LOW-MED (ongoing) | Post-cutover ha-2 is sealed; patching is an operator-initiated event. Stale `ca-certificates` eventually breaks A2's TLS; stale `tzdata` breaks local-time math. Spec exists (`provisioning/03-sneakernet-updates.md`), tooling greenfield. |
| **C2 · Firmware build artifacts** (ESP-IDF/PlatformIO/esphome) | N/A to ha-2 | Builds happen on **`.210`** (docker esphome). ha-2 never builds; OTAs are served inside the air-gap net. |

### D. Air-gap lifecycle time-bombs — not "internet services", but silently fatal over time
| Dep | Fuse | What breaks | Handling |
|-----|------|-------------|----------|
| **D1 · TLS cert expiry** | **Apr 5 2028** (verified) | Self-signed cert covers PWA (via `.210:443` bridge) + `ha-api-tls:8443`. On expiry, **all HTTPS fails** with no ACME to renew. `tools/gen_tls.py`. | Pre-generate a long-dated (or internal-CA) cert; **monitor `notAfter`**; scripted rotation deployed to both boxes. |
| **D2 · JWT signing key / 12 h token TTL** | rolling | `instance/auth_key` (HS256). Rotation is **abrupt** — live tokens just start failing; clients must `/auth/refresh`. `server/api/auth_tokens.py`. | Key-custody + documented rotation; ensure non-web clients refresh. |
| **D3 · Failover SSH key custody** | on loss | `.210↔ha-2` SSH keys drive fencing + all reconcile/sync. Lose them → failover + data plane both stall. | Back up keys off-box; documented recovery. |
| **D4 · DNS / hostname resolution** | latent | Cert SAN + some tooling reference `ha-dev`/hostnames; browser-by-hostname needs avahi/`/etc/hosts` (production paths already use IPs — safe). | Provide air-gap-local name resolution (hosts/avahi) if hostname UX is wanted. |
| **D5 · Dead web-push code** | now | Deprecated FCM/Mozilla push loop still runs ~60 s on ha-2, failing silently. `server/api/main.py`. | Clean up (also on the deny-list). |

### E. Verified NON-dependencies (confirmed local — do NOT relay; deny-list)
- **Midea dehumidifier** — **VERIFIED local-stable 2026-07-10**: token file is **21 h old** yet
  `dehumidifier_living_room` reports **fresh telemetry** → LAN token auth works well past the feared ~18 h
  cloud-rotation. Memory `midea-app-dep-control-vs-provisioning` confirmed over the misleading
  `failover/README.md` "~18h rotation" line (flag for doc fix). Levoit (ESPHome-reflashed), SwitchBot (BLE)
  likewise local. **Never relay Midea/Levoit/VeSync/SwitchBot clouds, FCM/Mozilla push, or (post-cutover) the
  provisioning hosts** — their presence would signal misconfig or exfil.

### F. The elephant — production self-sufficiency (separate, larger initiative)
- North star `prod-self-sufficient-is-the-goal`: ha-2 eventually runs hands-off, **no AI oversight**. Bootstrap
  installs Claude Code but it's **online-only**; there is **no on-device LLM**. True hands-off would add a whole
  dependency class (local model weights + their updates). ADR-0031 explicitly says hands-off is *not achieved*.
  ha-2's self-maintenance timers (rungs/rollup/compaction) **were installed 2026-07-10** (per checkpoint), so
  data-tier self-sufficiency is largely closed; the **LLM/hands-off** piece is the remaining north-star gap.
  **Out of scope for this relay**, but named so the picture is complete.

---

## 3. Design principles (security-focused)

1. **Application-layer relay, never IP forwarding.** `.210` *terminates* and *re-originates* each request. No
   NAT/masquerade `wlp2s0`→`enp4s0`, ever. `FORWARD` stays `DROP`. Air-gap net has **zero IP reachability to
   the internet**.
2. **`.210` default-deny on `wlp2s0`.** Host nftables DROPs all inbound on the air-gap iface except the few
   narrow relay ports; close `8123/8124/8443/8095`, restrict `ssh`.
3. **Per-service, typed, allow-listed relays — not a general proxy.** Each crossing is its own small audited
   channel with a fixed schema. No open forward-proxy for the air-gap net.
4. **Mediated request model over a scoped bus.** ha-2 never opens a raw socket to `.210`'s internet side — it
   publishes/receives on a scoped `relay/#` topic; the relay daemon validates, fetches from an **egress
   allow-list** (open-meteo, ntfy, NTP pool, update mirror only), returns a schema-checked result.
5. **The relay is hostile-adjacent → least privilege + rate-limited + audited.** It runs as a dedicated
   unprivileged uid, egress-restricted (nftables owner-match or a netns) so a relay/ha-2 compromise can't
   reach arbitrary hosts; every relayed request is **logged** (who/URL/size) and **rate-limited** (a
   compromised ha-2 must not weaponize it to DoS or drip-exfiltrate).
6. **The relay must itself be patched + monitored.** It's the internet-facing edge of the trust boundary — it
   gets a health check, an expiry/cert monitor (D1), and rides the `.210` update cadence.
7. **Fail-secure & fail-static.** Relay down ⇒ weather stops, clock holds last-good (`local stratum 10`),
   alerts queue on ha-2's bus. A failure must **never** widen exposure.
8. **Directionality separated.** IN relays (time/weather/updates) and OUT relays (ntfy/backups) are distinct
   channels with distinct policy — an inbound path can't be repurposed for exfil.

---

## 4. Concrete structure, per service

- **A1 NTP (fix first — lowest risk, half-built).** `.210` runs **chrony as a server on `wlp2s0`**, disciplined
  by the internet pool via `enp4s0`; router + ha-2 sync from `.210` (`.1.245`, which the router already
  targets). Reuse `provisioning/ntp/{chrony-ha-serve.conf,install-ntp-serve.sh 192.168.1.0/24}`. Firewall:
  allow **only `udp/123` from `192.168.1.0/24`**. `.210` is the anchor — ha-2 must *not* be left a
  `stratum 10` island with confident-but-wrong time. Add a chrony health/restart watch (A1 is control-critical).
- **A2 Weather.** Keep the open-meteo fetch on `.210`; publish canonical weather to `relay/weather/state` on
  the scoped bus; ha-2 ingests; `ha-weather.timer` stays disabled on ha-2. One-way-in, no open-meteo
  reachability from the air-gap.
- **B1 Notifications (toggleable — DECIDED).** ha-2 `home/_alert/new` → scoped `relay/alert/out` → `.210`
  `ha-ntfy-bridge` → ntfy → phone. `8095` stays **closed** to the air-gap net (ha-2 never touches it
  directly). Realizes the FOLLOWUPS "ha-2 → `.210` → ntfy" item. **Gated by `RELAY_NTFY_EGRESS` (default
  OFF = on-network-only).** The switch lives on `.210`'s ntfy-bridge (the egress point), so flipping it is a
  hot config change — no firewall/air-gap edit. When OFF, ha-2 alerts still surface on-LAN (BFF banner + the
  `home/_alerts` retained topic any on-network device can read); only the *outbound phone push* is suppressed.
- **B2 Backups + C1 Updates — sneakernet primary.** The `provisioning/03-sneakernet-updates.md` bundle:
  periodic, offline, auditable USB round-trip carries ha-2 backups OUT and vetted `.deb`/wheel/`tzdata`/CA
  bundles IN. Optional `.210` apt/pip mirror for routine patching in an explicit maintenance window. **No
  standing update path** — updates are an operator event, never ambient.
- **D1 Cert lifecycle (DECIDED).** Long-dated self-signed regen (`tools/gen_tls.py --days 3650`) + a
  `notAfter` monitor that alerts ~T-12mo. No internal CA (Hugh 2026-07-10). Rotation re-touches each client's
  trust store — accepted for simplicity.

---

## 5. The firewall (nftables on `.210`)

Inbound on **`wlp2s0`** — **default DROP**, allow only:
- `udp/123` (NTP) from `192.168.1.0/24`
- `tcp/1883` (relay/failover broker — the `relay/#` + alert bus) from `192.168.1.0/24`
- `ct state established,related`; optional ICMP echo for health
- **`ssh/22`**: drop on `wlp2s0` (admin from the household side) — or key-only + single mgmt-host allow-list
- **Explicitly closed to the air-gap net:** `8123`, `8124`, `8443`, `8095`

Outbound on **`enp4s0`**: the **relay daemon's uid** is egress-allow-listed to open-meteo, ntfy upstream, NTP
pool, update mirror only (nftables owner-match / netns) — narrower than `.210`'s general dev egress.
**Never** a `wlp2s0`→`enp4s0` forward/masquerade rule; `FORWARD` stays `DROP`. Stage the ruleset with a
**timed auto-rollback** so a bad rule can't lock out the air-gap admin path.

---

## 6. Already-built vs greenfield (scope)

**BUILT / reusable substrate:** bidirectional sensor reconcile (`reconcile-history.sh`, `reconcile-parquet.sh`),
archive-parity hard gate (`provision-peer.sh`), failover signaling (`notify.sh` + keepalived), the scoped
**MQTT cluster bridge** `ha/cluster/#` (device `home/#` *deliberately* not bridged — the exact pattern
`relay/#` should follow), `ntfy_bridge.py`, `router_reconcile.py`, and the **sneakernet bundle spec**.

**GREENFIELD (the lift):** NTP server on `.210` + health watch · nftables default-deny on `wlp2s0` · weather
relay daemon · ntfy egress from the air-gap · scoped `relay/#` bus + ACLs · relay least-priv/rate-limit/audit ·
cert-rotation + expiry monitor · USB backup/restore tooling + "nag" UX · optional `.210` mirror.

**Phased rollout (each independently revertible, Hugh-gated):**
1. **NTP serve + health watch** — fixes a *live* breakage; smallest blast radius. *(~days)*
2. **nftables default-deny on `wlp2s0`** — the "mostly port-closed" core; timed auto-rollback. *(~days)*
3. **Weather relay** (publish → ingest). *(~week)*
4. **ntfy egress relay** (ha-2 alerts → phone). *(~week)*
5. **Sneakernet backup/restore + optional mirror** + cert-rotation tooling. *(~1–2 weeks)*

Rough total **~4–6 weeks**, matching the "bigger lift" read. Each step: built in code
(`provisioning/airgap/relay/…`), documented, verified, committed — no bench-only tweaks.

---

## 7. Open questions for Hugh

1. **Relay bus:** scoped `relay/#` ACL topics on the existing broker (simpler) vs a **dedicated minimal relay
   broker** (cleaner isolation, more parts)? *Recommend scoped ACLs first.*
2. **Air-gap-side SSH to `.210`:** fully close `22` on `wlp2s0` (admin via household side) vs key-only +
   single-host allow-list? *Recommend close it.*
3. **Updates:** sneakernet-only, `.210` mirror-only, or both? *Recommend both, sneakernet primary.*
4. **Weather:** push-in (simple, one-way) vs request/response? *Recommend push-in.*
5. ~~**Cert (D1):** self-signed long-dated regen vs internal CA?~~ **DECIDED (Hugh 2026-07-10):** long-dated
   self-signed (`--days 3650`) + `notAfter` monitor at T-12mo. No internal CA.
6. **First PR scope:** just Phase 1+2 (NTP + firewall — the security core), then iterate the data relays?
7. **Self-sufficiency LLM (F):** track as a separate initiative, or fold a "how does hands-off get its brain
   air-gapped" section into this ADR?
