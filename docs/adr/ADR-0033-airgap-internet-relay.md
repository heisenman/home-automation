# ADR-0033 — Air-gap internet-service relay & data-transfer (`.210` as a narrow, hardened pass-through)

**Date:** 2026-07-10 · **Owner:** dev · **Decider:** Hugh · **Status:** Accepted (phased build in progress)
**Detailed design log:** [`../airgap/AIRGAP-RELAY-DESIGN.md`](../airgap/AIRGAP-RELAY-DESIGN.md) (the full picture —
threat model, complete dependency map, per-service structure, firewall, break-glass, air-gapped LLM).
**Relates to:** ADR-0031 (air-gap failover), `provisioning/03-sneakernet-updates.md`, `docs/FOLLOWUPS.md`.

## Context

The production dictator **ha-2** is **air-gapped by design** (`192.168.1.210`, no internet — the security
property we protect). A handful of internet-derived services it still needs (authoritative **time**, weather,
off-network **notifications**, OS/security **updates**, off-site **backups**) can only reach it through
**`.210`** — the *one* dual-homed box touching both the internet (household `enp4s0`) and the air-gap net
(`wlp2s0`). That makes `.210` the **crown jewel**: compromising it is the only software path that defeats the
air-gap.

Two exhaustive dependency sweeps + live verification established: the *service count* is small (only NTP,
weather, and a notification egress are live internet dependencies; Midea/Levoit/SwitchBot are **verified
local**, not cloud), but the *lift is not* — the relay must be a **hardened, bidirectional subsystem**, and
there are **air-gap lifecycle time-bombs** (TLS cert expiry Apr 2028, key custody) that silently kill an
isolated box over months. `.210` today is **wide open to the air-gap net** (SSH/8123/8124/8443/ntfy all bound
`0.0.0.0`, no host firewall). NTP was already **broken** (ha-2 had zero reachable sources).

## Decision

Build `.210` as a **narrow, mostly-port-closed, application-layer relay** — never an IP router — governed by:

1. **Application-layer termination, never IP forwarding** (`FORWARD` stays `DROP`; no NAT air-gap→internet).
2. **`.210` default-deny on `wlp2s0`** (nftables) — open only the few narrow relay ports; close
   `8123/8124/8443/8095`; SSH allow-listed to ha-2 only.
3. **Per-service, typed, allow-listed relays** over a **dedicated minimal relay broker**; **request/response**
   (ha-2 pulls on demand → stochastic egress, traffic-analysis resistance).
4. **The relay is hostile-adjacent**: least-privilege uid, egress allow-list (open-meteo/ntfy/NTP/mirror only),
   rate-limited, audited, and itself patched + monitored.
5. **Fail-secure / fail-static**; **directionality separated** (IN vs OUT channels).

**Ratified specifics (Hugh 2026-07-10):** dedicated relay broker · close `wlp2s0` SSH = **allow-list ha-2**
(blanket close would break failover SSH — see follow-up) + **signed break-glass USB** recovery · updates =
**both** sneakernet (default) + `.210` mirror (**disabled by default in git**) · weather = **request/response** ·
ntfy egress = **kept but toggleable** (`RELAY_NTFY_EGRESS`, default OFF) · TLS cert = **long-dated self-signed
(`--days 3650`) + `notAfter` monitor** (no internal CA) · **front-load the security core** · **fold in** the
air-gapped-LLM question (design-log §8).

## Rollout (phased, each revertible)

1. **Security core (front-loaded):** (a) ✅ **NTP serve on `.210` + air-gap clients** — *DONE 2026-07-10*;
   (b) dedicated relay broker + daemon scaffold; (c) nftables default-deny on `wlp2s0` (staged, auto-rollback);
   (d) signed break-glass USB recovery.
2. Weather request/response relay.
3. ntfy egress relay (toggle, default OFF).
4. Sneakernet backup/restore + `.210` mirror (disabled-by-default) + cert rotation/monitor.

### Phase 1a — NTP (DONE 2026-07-10)
`.210`'s chrony (internet-disciplined) now **allows the air-gap net** (`192.168.1.0/24`, was household-only →
501-refused ha-2); ha-2 points at `.210` (`192.168.1.245`) instead of the unreachable internet pool. Verified
live: **ha-2 `System clock synchronized: yes`**, `Reference ID = 192.168.1.245`, offset slewed +910ms → sub-ms.
Code: `provisioning/ntp/{chrony-ha-serve.conf (both allows), install-ntp-serve.sh (multi-CIDR),
chrony-airgap-client.conf (new)}`. Follow-on: a chrony health/lost-sync monitor (A1 is control-critical).

## Consequences

- **Positive:** the air-gap's one bridge becomes narrow, auditable, and default-deny; ha-2 gets the internet
  it needs without ever touching the internet; time (control-critical) is fixed today.
- **Costs / risks:** `.210` remains the highest-value target (mitigated by least-privilege + monitoring); the
  SSH allow-list vs blanket-close tension is deferred to a follow-up (switch port / drop SSH dependency);
  cert rotation re-touches client trust stores; the air-gapped-LLM north star is a separate multi-week effort.
- **Non-goals / deny-list:** vendor clouds (Midea/Levoit/VeSync/SwitchBot), Web Push (FCM/Mozilla), and — post
  cutover — the provisioning hosts are **never** relayed; their appearance signals misconfig or exfil.
