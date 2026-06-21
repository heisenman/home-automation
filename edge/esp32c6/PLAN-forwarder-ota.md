# Edge Node — Generic GATT Forwarder + OTA (build plan)

**Status:** planned (2026-06-21). Build requires the C6 back at the desk (USB flash).

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

## ⚠️ Brick-safety — open consideration (user, 2026-06-21)
**Question to answer before ANY deployed-node OTA: are the post-OTA-provisioning code updates unlikely
to remotely brick a node?** Required safeguards (design in *before* shipping OTA):
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
