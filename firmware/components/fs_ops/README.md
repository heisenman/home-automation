# fs_ops — SD file-ops over MQTT (ADR-0020)

A small remote filesystem surface so an SD card can be driven live over the bus — no
reflashing to inspect or seed files. Commands arrive as JSON (a device wires its own
`cmd/fs` topic to `fs_ops_submit`); each runs on a worker task off the mqtt-callback
stack (the v11/v17 blocking-IO lesson) and a JSON result goes back through the caller's
publish sink.

## Ops
`ls | stat | read | write | rm | mkdir | df` — JSON in/out, base64 for file bytes,
chunked `read` (off/len), scoped to `/sdcard` for safety. See `include/fs_ops.h`.

## Platform support
Any target with a FAT filesystem mounted at `/sdcard` (see `ha_sdcard`) and an MQTT
publish callback. No board-specific code.

## Consumed by
- d1001-panel (reTerminal D1001) — first adopter.
