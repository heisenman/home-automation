# ADR-0002 — Capability Contract (Trait Vocabulary)

**Date:** 2026-06-19  
**Status:** Accepted

## Decision

Devices are described by a small vocabulary of traits (`switchable`, `ranged`, `positionable`,
`lockable`, `setpoint`) rather than product names. Policies target traits, not products.

## Context

As new hardware arrives, device-specific admin logic creates O(n) coupling. A uniform
capability layer keeps the admin/policy surface fixed as the device fleet grows.

## Consequences

- Bespoke driver implementations are allowed *below* the trait interface
- The trait interface must not leak upward into dictator/admin logic
- New devices can be inducted at the generic level immediately; bespoke drivers are
  an optimization, not a requirement
- Phase 5 implementation: registry schema, policy store, trait validators

## Rejected alternatives

- Product-specific admin logic: requires code changes for each new device class
- Full HomeKit/Matter ontology: more than needed for a single-home system;
  adds dependency and versioning complexity
