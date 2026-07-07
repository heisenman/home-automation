# BREADCRUMB: server > control - actuator control loop, override/policy/scenes, trait-based signed command issuance. Contract: 0002,0011,0014. Parent: server/AGENTS.md.
# REUSE-WHEN: you're actuating a device or resolving a control policy/scene — go through the trait loop + signed issuer
"""Command-and-control plane (Phase 5/6).

The dictator's control side: a small trait vocabulary (ADR-0002), a versioned/validated policy
store with guardrails + standing orders + whole-house modes (plan §11), and a single Policy
Enforcement Point that authorises every command before it reaches a device (ADR-0001 / plan §13).

Read-only sensing is unaffected; this package is the *control plane*. Nothing here lets a raw
sensor reading drive an actuator — commands originate only from authorised requests through the PEP.
"""
