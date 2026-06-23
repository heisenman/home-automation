# ADR-0010 — Command & Control Protocol (Authenticated Directives)

**Date:** 2026-06-21
**Status:** Accepted — **fully implemented** (reconciled 2026-06-23). Server-side + protocol built;
**broker auth/ACL cutover done** (2026-06-21); **node-side enforcement live** in firmware `v9-bankts`
(signed-directive verify + GATT-write lockdown + OTA host-pin + image-hash verify). Remaining: per-node
nonce/counter for the 60s ts-replay window (deferred).

## Decision

Every directive the dictator sends to a node — actuator commands **and** firmware OTA — is an
**authenticated, signed message** verified at the device before it acts. Authentication uses
**HMAC-SHA256 with a per-device secret** (the per-device credential of plan §13.3), not PKI.

A command carries: protocol version, id, target device/node, trait, action, validated args, a
server-minted **freshness nonce**, a **sensitive** flag, a **timestamp**, and a **signature** over
all of the above. The node verifies signature (constant-time), timestamp freshness, and — for
sensitive actions — that the nonce has not been replayed. Anything failing is refused.

OTA is the same shape: the directive signs `url + sha256 + version`. The node verifies the
signature (origin), then verifies the **downloaded image's SHA-256 against the signed hash**
(integrity) before flashing, and refuses a **downgrade** (soft, NVS-based — never eFuse-locked, so
USB recovery is preserved per ADR-0005).

All commands flow through the single **Policy Enforcement Point** (the issuer, ADR-0001): resolve →
evaluate policy (deny stops here, nothing is sent) → sign → send → await ack → reconcile intended
vs reported state. Humans never sign or touch devices directly.

## Context

The generic GATT forwarder + OTA (2026-06-21) made the edge node carry **authority** (arbitrary GATT
writes can drive any controllable BLE device; OTA replaces firmware) — yet the command path was an
anonymous MQTT topic: any LAN host could publish a command or an arbitrary-URL OTA, and the node
would execute it. Rollback protected against *bricking* but not a malicious-but-functional image.
The control plane (Phase 5/6) therefore cannot be built on an unauthenticated channel — the PEP is
meaningless if the device executes whatever lands on its topic regardless of origin.

HMAC over PKI: the system is **offline-first** and nodes are MCUs. HMAC-SHA256 verification is cheap
(mbedtls is already linked on the C6 for OTA TLS), per-device secrets fit the physical-presence
enrollment model, and there is no CA/cert lifecycle to run air-gapped. The threat model (plan §13.4)
is passive-sniffing + rogue-LAN-device resistance, which signed+nonce'd directives + broker ACLs
cover; we are explicitly **not** hardening against a determined active attacker.

## Consequences

- A forged or replayed directive is refused **at the device**, even on today's anonymous broker,
  because the attacker lacks the per-device secret. (Proven: 29 unit/loop tests + a live broker demo
  where a wrong-secret command is rejected `bad-sig` and leaves device state unchanged.)
- Per-device secrets must be **enrolled at the server console** (physical presence) and stored in a
  gitignored secrets store; rotation is per-device.
- **Defense in depth, still required:** broker auth + topic ACLs (only the dictator identity may
  publish any `…/cmd`; nodes pub only own telemetry, sub only own cmd/reply) + local-CA TLS. The
  signature authenticates the *directive*; ACLs/TLS protect the *channel*. Both are needed.
- **Node-side enforcement is pending firmware:** the deployed C6 does not yet verify (it still runs
  unsigned commands / unsigned OTA). Closing this is itself OTA-deliverable: push the verify-capable
  firmware once over the current path, then all subsequent directives must be signed.
- **Broker auth is a coordinated cutover** (services + node + tools need credentials at once) — done
  deliberately, not flipped live unsupervised.
- Freshness depends on synced clocks (plan §10, server is time authority); the default window is 30 s.

## Rejected alternatives

- **PKI / per-device certificates:** CA lifecycle and revocation are heavy for an offline single-home
  system on MCUs; HMAC per-device secrets give per-device authn + revocation (drop the secret) with
  far less machinery. (Revisit only if a node must prove identity to a third party.)
- **TLS client-certs as the only control gate:** protects the channel but not directive provenance
  across a compromised broker; and offline cert management is the same PKI burden. Keep TLS as channel
  defense, keep the signature as the directive's authenticity.
- **Rollback-only OTA safety:** stops bricking, not malice — a malicious image that connects Wi-Fi +
  MQTT passes the self-test. Signed image hash is required for authenticity.
- **Trusting broker ACLs alone:** ACLs gate *who can publish*, but a signed directive additionally
  survives a broker compromise and gives non-repudiation. Belt and suspenders for authority ops.
