# Phase B — `relay_assign` directive contract (ADR-0015 Tier 2)

*The fixed contract between the server coordinator (ops) and the edge firmware (dev). Locked 2026-06-24.
Honors ADR-0015 decisions #2 (default relay-all), #3 (dedicated `/relay`, signed), #4 (own service), #5
(event + ~15-min re-negotiation).*

## Purpose
Tier-1 (the mapper, LIVE) already drops redundant readings **server-side**. Tier-2 stops the redundant
**transmission at the node**: the dictator tells each edge node the exact set of devices it should relay
(the ones it's the preferred source for), so it stops broadcasting meters a closer receiver already covers
→ saves edge radio/CPU/PoE energy. Default-relay-all means an un-provisioned node is useful immediately.

## Transport
- **Topic:** `home/edge/<node>/relay` — **retained, QoS 1**. Retained so a rebooting node re-reads its
  current assignment without waiting for the next re-negotiation.
- **Signing:** the **same `{p, s}` signed envelope the firmware already verifies on `/cmd`** —
  `s = HMAC-SHA256(node_secret, p)` over the literal `p` string; `p` is the JSON payload below as a string.
  Firmware reuses `cmd_sig_ok()` verbatim (just a second subscription). Empty `HA_CMD_SECRET` ⇒ reject
  (enrolled nodes only). Integrity is mandatory (decision #3) even on the trusted LAN.

## Payload (`p`, a JSON string inside the envelope)
```json
{ "schema": 1, "type": "relay_assign", "epoch": 7,
  "relay_macs": ["B0:E9:FE:54:AB:A2", "..."],   // ALLOWLIST: relay ONLY these BLE MACs; drop all others
  "cmd_relay":  [],                              // actuator device_ids this node relays cmds to (future; empty now)
  "ttl_s": 3600 }
```
- **`relay_macs`** — the server's registry resolution of the device_ids this node is the preferred source
  for, to BLE MACs (what the node's adv filter matches). The node relays an advert **iff** its MAC ∈ this set.
- **`epoch`** — monotonic per node. The node **ignores any epoch ≤ the last applied** (idempotent, ordered).
- **`cmd_relay`** — reserved for downlink actuator-command relay (ADR-0010); empty until the first BLE actuator.
- **`ttl_s`** — advisory; the retained directive is the source of truth, re-pushed on change.

## Firmware behavior (dev's half — `adr15-phase-b-firmware`)
1. Subscribe `home/edge/<node>/relay` (reuse the `/cmd` envelope-verify path).
2. On a valid, newer-epoch directive: persist `{epoch, relay_macs}` in **NVS** (survives reboot); apply.
3. **Relay filter** in `ble_scan`: publish an advert only if its MAC ∈ `relay_macs`.
4. **Default before any directive: relay-all** (today's behavior) — backward-compatible.
5. Reboot: load NVS allowlist+epoch and resume filtering immediately (retained directive re-confirms).

## Server behavior (ops's half — `ha-relay-coordinator`)
- Compute per-node allowlists from the **`mesh.db` reach graph** (`best_relay` over `server/mesh`) + the
  registry (device_id→MAC). A node's allowlist = the devices it is the preferred source for.
- Sign per-node with the node's enrolled secret (from the master-decrypted LUT — runs on the dictator only).
- Publish retained on `/relay`; bump `epoch` only when a node's set changes (re-push only changed — decision #5).
- **Re-negotiation:** on node up/down + sustained coverage shift, plus a ~15-min periodic backstop (debounced).
- **Default OFF / dry-run** until the firmware consumer exists: `--dry-run` prints the directives it would send.

## Open tuning note — RESOLVED (2026-06-24)
The original concern (`best_relay` over-preferring `local`, leaving the S3 allowlist near-empty) is **resolved
by the adv-reception RATE signal** (`adv_score`, commit 25407a0): the coordinator now weighs *how often* a
source is heard, not just last-seen age, so a steadily-heard edge node beats a gappy-but-recent local. Soak-
validated on live 210: s3 wins 6 meters, c6-bench 2, local only `aranet_radon` + genuinely-closer meters —
matching the live sniff. So edge allowlists are non-empty and reflect real reliability.

## Implementation status — server half (2026-06-24)
BUILT + unit-tested (offline), **not yet publishing live**:
- `sign_envelope()` — the `{p,s}` firmware envelope (HMAC over literal `p`); `publish_pass()` signs per-node
  from the master-decrypted LUT and publishes retained QoS1 on `/relay`.
- `reconcile()` — decision #5 debounce: a changed allowlist must hold `--dwell` (default 900 s) before
  re-publish; monotonic per-node `epoch` bumped only on a committed change; `relay_state` persists across runs.
- CLI: `--publish` (live), `--loop N` (periodic backstop), default **dry-run**.
- **Gated rollout** (needs Hugh GO): dry-run preview on 210 → `--publish` canary on ONE node (short dwell) →
  confirm firmware applies (NVS epoch + filtered relay) → full + a systemd timer for the periodic backstop.
