"""Communication-event layer (ADR-0012).

A normalized, transport-agnostic vocabulary for connection/health events emitted by every transport
(BLE adv/GATT, MQTT-to-node, Midea LAN, ...) and consumed by everyone (controller fail-safe, mesh
reroute, UI health badges). `events` is the pure vocabulary + classification + health derivation;
`recorder` is the thin I/O (persist to comms_event + publish to MQTT home/_event/<device>).
"""
