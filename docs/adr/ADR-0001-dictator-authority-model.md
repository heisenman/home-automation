# ADR-0001 — Dictator Authority Model

**Date:** 2026-06-19  
**Status:** Accepted

## Decision

Single authoritative server ("the dictator") with a warm standby. No peer consensus, no
multi-master. All device communication routes through the server; clients never touch
devices directly.

## Context

Home automation systems typically distribute control across devices or use vendor clouds.
Both create availability/correctness failures: distributed consensus is complex to
implement correctly; clouds remove offline operation.

## Consequences

- One broker (Mosquitto) on the dictator; all nodes subscribe/publish through it
- Failover via VRRP floating VIP (one holder at a time) — Phase 7
- Standby is warm-but-mute until it holds the VIP
- Simplifies security: one hub to harden

## Rejected alternatives

- Multi-master / peer consensus: unnecessary complexity for a house; split-brain is a
  real risk and not worth the marginal availability gain
- Vendor cloud coordination: violates offline-first requirement
