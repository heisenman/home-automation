# ADR-0010 — Command & Control Protocol (Authenticated Directives)

**Date:** 2026-06-21
**Status:** Accepted — **fully implemented** (reconciled 2026-06-23; replay-nonce added 2026-06-25).
Server-side + protocol built; **broker auth/ACL cutover done** (2026-06-21); **node-side enforcement
live** in firmware `v9-bankts` (signed-directive verify + GATT-write lockdown + OTA host-pin +
image-hash verify). **Per-node anti-replay now CLOSED:** the firmware freshness window (300 s for
gatt/history, 86400 s for ota — not the 60 s an early docstring claimed) is paired with a per-node
**monotonic `(ts, seq)`** guard, persisted in NVS (namespace `ha_cmd`): a node acts on a signed command
only if its `(ts, seq)` is strictly newer than the last it acted on, so a captured command can't be
replayed inside the window. `ts` is server-stamped (monotonic regardless of node clock drift) so the
scheme self-heals across a dictator rebuild; `seq` (added by `tools/edge_sign.py`) orders commands within
one second. Shipped firmware `v11-nonce` (C6) / `v14-nonce` (S3) / `v10-nonce` (C3-fork).

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
- **Node-side enforcement is LIVE** (reconciled 2026-06-23): the C6 runs `v9-bankts`, which verifies the
  HMAC signature on every directive (incl. OTA), refuses GATT writes by default, pins the OTA host, and
  hash-verifies OTA images. (Originally written "pending firmware: the deployed C6 does not yet verify" —
  that was true at authoring; it shipped since.)
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
