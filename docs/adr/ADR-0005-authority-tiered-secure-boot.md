# ADR-0005 — Authority-Tiered Secure Boot

**Date:** 2026-06-19  
**Status:** Accepted — Phase 8

## Decision

Secure Boot hardening tracks node authority. Sensor-relay nodes (no actuator control)
skip Secure Boot; authority-bearing nodes (lock controllers) get Secure Boot + cable-only
flashing. eFuses are never irreversibly locked on any USB-recoverable node.

## Context

Secure Boot prevents physical reflash by an attacker. For a sensor relay, the blast
radius of compromise is a spoofed temperature reading (caught by corroboration, §13.7).
For a lock controller, physical reflash is a direct security bypass. The protection cost
is proportional to the stakes.

## Consequences

- Sensor nodes: signed OTA + per-device credential + central revocation is the
  proportionate control; keep cheap spares for brick recovery
- Authority nodes: Secure Boot + cable-only flash + dedicated spares
- eFuse locking: never irreversibly lock — preserves USB esptool recovery path
- Wasm sandbox on sensor nodes means a compromised peripheral module cannot escalate
  to the credential/lock layer regardless

## Rejected alternatives

- Secure Boot everywhere: overkill for sensor nodes; irreversible eFuse burn can
  permanently brick nodes; proportionality argument is strong
- No Secure Boot anywhere: acceptable for Phase 1–7 sensors-only; required before
  any lock controller is commissioned
