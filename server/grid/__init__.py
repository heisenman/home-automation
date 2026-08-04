# BREADCRUMB: server > grid - voluntary grid load-shed lane (curtail actuators on a utility event). Contract: 0037. Parent: server/AGENTS.md.
# REUSE-WHEN: you need to curtail devices on an external signal — extend a ShedSource, don't add a second control path
"""Voluntary grid load-shed lane (ADR-0037) — runs on .210, the only box that sees both the public
internet (shed signal) and the air-gapped control plane (ha-2, which holds the VIP and the actuators)."""
