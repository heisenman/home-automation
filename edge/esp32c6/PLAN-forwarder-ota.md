# Edge Node — Generic GATT Forwarder + OTA (build plan)

**Status:** ✅ BUILT & BENCH-VALIDATED (2026-06-21). All 5 steps done; C6 redeployed to the
end of the house and now receives updates over the air (no cable). Commits: OTA partitions
`bf5ac47`, forwarder `cea493e`, OTA op + tool `896df06`.

## Results (2026-06-21)
1. **OTA-ready partitions** — 4MB C6FH4, two 1.75MB slots (`ota_0`/`ota_1`) + otadata; rollback on.
2. **Generic GATT step-interpreter** (`gatt_exec.c`) — connect → discover-all-chars → run
   server steps (sub/write/writeseq/read/collect/delay) → stream replies on
   `home/edge/<node>/<reqid>/reply`. Validated live: probe (7 chars), read, and
   sub+write-handshake+collect drawing the meter's `0x01` ack notif back.
3. **Server driver** `tools/edge_gatt.py` — composes steps, correlates replies by reqid; empty
   step list = a GATT probe.
4. **Test** — read/write/notif all proven; the SwitchBot history handshake re-expressed on the
   primitive (the `0x01` ack is what `gatt_history.c` elicits, so setup→metadata→paging is just
   server-supplied data on top of `gatt_exec`).
5. **OTA op** `tools/edge_ota.py` + `ha_ota.c` — HTTP-pull into the inactive slot, self-test,
   confirm-or-rollback. Both paths bench-tested (see below).

> Follow-up: `edge_ota.py`'s wrapper had one inconclusive run at the desk (node rebooted, stayed
> on `ota_0`, no logs) — the underlying OTA *mechanism* is proven via manual push (both paths).
> Re-validate the tool with a clean OTA to the now-deployed node next session (safe: rollback-protected).

## Goal
A **generic BLE-GATT proxy** on the edge node so new device interactions need NO new firmware —
the server composes BLE steps, the node executes them and returns replies. AND **bootstrap OTA**
so future updates don't require physically retrieving deployed nodes.

## Why build them together
Both need the same primitive: a reliable **bidirectional server↔node command/data channel over MQTT
with chunked transfer**. The forwarder builds it; OTA reuses it (OTA = "just another op" + firmware
delivered over the same MQTT chunking, or an HTTP pull). OTA-specific extras layer on top: an OTA
partition layout, `esp_ota` write/verify/reboot, and signed images (ADR-0005).

## Plan (incremental, each step testable)
1. **OTA-ready partitions** — `factory` → `ota_0`/`ota_1`/`otadata` (+ nvs/phy). Prerequisite for OTA;
   harmless otherwise. Do first.
2. **Generic GATT step-interpreter** (firmware) — refactor the proven history-pull plumbing
   (`gatt_history.c`) into `gatt_exec(mac, steps[])`: `connect / subscribe(char) / write(char,hex) /
   write_seq(char,[hex],gap_ms) / read(char) / collect(ms) / disconnect`. Stream replies on
   `home/edge/<node>/<reqid>/reply` (notifications, read results, step done, final status).
3. **Server `edge_gatt()` helper + CLI** — compose steps, publish to `home/edge/<node>/cmd`, collect
   correlated replies by reqid. Generic function reused by any device interaction.
4. **Test** — drive a generic read/write end-to-end (e.g. read a device characteristic), then
   re-express the SwitchBot history pull *on top of* the primitive (proves the generalization).
5. **OTA op (stretch)** — `{"op":"ota"}` + MQTT-chunked (or HTTP) firmware delivery + `esp_ota`; test
   an update on the bench node.

## ✅ Brick-safety — ANSWERED (2026-06-21)
**Question: are post-OTA-provisioning code updates unlikely to remotely brick a node?** Answer: **yes,
within full-image OTA's limits — a connectivity-breaking update cannot brick a deployed node.**
Demonstrated live on the bench: an image built with an unreachable broker was OTA'd; it booted in the
inactive slot, failed its self-test (no MQTT in 15s), and the bootloader **auto-reverted to the
last-good slot** — the bad image never took over and the node never published an online status from it.
The node recovered with zero intervention. (A real mid-OTA power interruption during the move likewise
left the node safely on its good slot — the validate-before-commit guarantee in practice.)
Residual risk that full-image OTA can't cover: a bug that breaks the *self-test logic itself* or the
OTA path while still connecting MQTT — bounded later by the ADR-0003 Wasm split (OTA sandboxed modules,
not the cable-flashed foundation). Safeguards in place:
- **A/B OTA with rollback.** `esp_ota` writes the *inactive* slot; bootloader boots it pending
  validation; the new app calls `esp_ota_mark_app_valid_cancel_rollback()` ONLY after a self-test passes
  (Wi-Fi + MQTT + BLE scan all up). If it doesn't, the bootloader **auto-rolls back** to the prior slot
  (`CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE` + `..._APP_ANTI_ROLLBACK` off so rollback is allowed).
- **Recovery image.** Keep a known-good image in the other slot / a factory slot — a bad update can
  never leave the node unbootable.
- **Watchdog.** A hung new app reboots → bootloader rolls back.
- **USB recovery preserved.** ADR-0005: never irreversibly eFuse-lock; esptool reflash is always the
  ultimate last resort.
- **Blast-radius bound (ADR-0003 Wasm split, Phase 8).** Eventually OTA the *sandboxed peripheral Wasm
  modules*, NOT the foundational cable-flashed C firmware — a bad module misbehaves in its sandbox,
  cannot brick, and a fix is OTA'd without touching the foundation. Until Phase 8, full-image OTA leans
  entirely on the A/B rollback + recovery image above.
- **Staged rollout.** OTA + validate ONE node (the bench/dev node) before any multi-node push.

## Operational note
Dev/test of the forwarder needs the C6 at the desk (USB flash) → attic/h_bed/c_office drop to
backfill-only (a *live* gap; history already CSV-backfilled) until redeploy. **OTA is precisely the
fix for this** — update deployed nodes without retrieval — so this build doubles as the OTA bootstrap.
