"""Unified control law — temperature, humidity and air quality across every actuator (docs/design/unified-control-law.md).

Stages L1–L3 (2026-11-12): settings (`bands`), actuator models (`models`), the pure solver (`solve`) and the shadow
runner (`shadow`). Nothing here commands a device: every actuator is enrolled `rule` (today's policy runs) or
`shadow` (the law's decision is logged next to the rule's). Switching a device to `law` is stage L4, per device,
with Hugh's approval.
"""
