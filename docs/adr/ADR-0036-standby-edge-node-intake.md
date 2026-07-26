# ADR-0036 — Standby edge-node intake (self-provision + discover + relocate)

**Status:** Accepted (2026-07-26, Hugh). Interactive design session; decisions recorded
inline below. Supersedes the flash-time-only enrollment assumption of ADR-0020 for *new*
standby hardware (already-enrolled nodes are unaffected).

**Directive origin:** Hugh, 2026-07-26. After confirming a BME680 ESP32-C6 (`standby_c6`)
was physically plugged into `.210` but did **not** appear in the PWA "Add device" flow:
*"standby devices are a good idea but they need to be discoverable for intake. What needs
to happen such that I can plug in a standby device (C6, S3, any MCU that's been
firmware-burned to the system) and see it in the PWA?"*

**Builds on:** ADR-0020 (edge node identity gate / enroll), the BLE discovery+intake path
([`server/ingest/discovery.py`](../../server/ingest/discovery.py), shipped @dc6ccec), and
`device_relocate` ([`server/maintenance/device_relocate.py`](../../server/maintenance/device_relocate.py)).

---

## Context — the gap

The PWA "Add device" modal has two candidate sources that were conflated:

- **The live discovery panel** (`GET /api/v1/discover`) is **BLE-only**. `discovery.py`
  subscribes to `home/unknown/+/raw` — unregistered *SwitchBot BLE adverts*. A WiFi/MQTT
  edge node has **no path** into it.
- **The type dropdown** (`SENSOR_TYPES`, `app.js`) lists `bme680_gas` and allows a manual
  MAC + `POST /api/v1/devices` — but that registers a **MAC-keyed sensor record**. An edge
  C6 gas node is keyed by a **synthetic node tag** (`meta.mac = "STANDBY_C6-GAS"`,
  `meta.node = "standby_c6"`) and is *activated by area assignment*, not MAC registration.
  So the manual path produces a half-wired record with no data.

An edge node's entire "I exist" signal today is a retained LWT string on
`home/edge/<node>/status` = `"online <slot> <fwver>"` (liveness only). Nothing turns *"an
online, unassigned node"* into an intake candidate, and the node never says **what** it is.

Deeper gap surfaced in design (Hugh): under ADR-0020 a node's per-device `cmd_secret` is
minted by `enroll_node.py` and written to **both** the node's `secrets.h` *and* the
dictator's LUT *before flash*. So "plug in a burned MCU" really means "plug in a MCU burned
*with its identity already baked in*." True plug-and-adopt needs the identity to be
established **by the act of intake**, not pre-seeded.

---

## Decision

Three thin layers, plus a change to **when/where the secret is born**.

### Layer 0 — Secret provisioning: node-born, registered at intake (the model change)

The unit is the source of truth for its own secret; the dictator **learns it at intake**.

1. **Generic image, no per-node secret.** One firmware image flashes to any C6/S3/… .
2. **Self-generate after WiFi is up.** On first boot, if NVS holds no `cmd_secret`, the node
   generates a **256-bit** secret and persists it to NVS. **Timing is load-bearing:**
   `esp_random()` is only a true hardware RNG once the radio is initialised — so we
   generate *after* `ha_wifi_connect()` succeeds, never at cold boot. `app_main` already
   brings WiFi up before MQTT, so this slots in naturally.
   - Construction: `secret = HMAC_SHA256(esp_random_32B, base_mac)` — the 256-bit random is
     load-bearing; the MAC is a **domain separator** for uniqueness-by-construction (Hugh's
     insurance). We do **not** derive the secret *from* the MAC alone (public → predictable).
3. **Prefer NVS over compiled-in.** `ha_config`/`ha_mqtt` read the NVS secret first; the
   compiled `HA_CMD_SECRET` stays only as the legacy path for already-enrolled nodes.

Why not worry about secret **collision**: at 256 bits the birthday bound sits near 2¹²⁸
nodes (~10⁻⁷⁰ for a home fleet) — chance-collision is off the table *provided the RNG is
well-seeded*, which is exactly what the "generate after radio-up" rule guarantees.
Dead-end recorded: seeding *from* the MAC to "avoid collisions" would trade secrecy for a
guarantee we already have; mixing the MAC in as a domain separator keeps both.

### Layer 1 — Node self-announces identity (firmware)

On every `MQTT_EVENT_CONNECTED`, in addition to the existing retained `status`, publish a
**retained** identity doc to **`home/edge/<node>/hello`** (identity only — **never** the
secret on a broadcast topic):

```json
{"schema":1,"node":"standby_c6","chip":"esp32c6","mac":"a0:f2:62:86:dc:4c",
 "fw":"v24-hello","slot":"ota_1","abilities":["bme680_gas"],"enrolled":false}
```

`chip` from `esp_chip_info()`, `mac` from `esp_read_mac()`, `abilities` from a new
`ha_mqtt_cfg_t` field the board sets from its compile-time sensor select. `enrolled`
reflects whether the dictator has claimed it yet. Retained so a late-subscribing server
sees it without a reboot.

### Layer 2 — Server edge-node discovery source

Sibling to `DiscoveryCache`: subscribe `home/edge/+/hello` (identity) + `home/edge/+/status`
(liveness), TTL rolling cache. A node is an **intake candidate** when online AND (its
`nodes.yaml` `area` is `standby`/unassigned, OR its `node_id` is unbound in the registry).
Identity from `hello` when present, else filled from the `nodes.yaml` manifest (so
already-enrolled standby nodes surface even on pre-`hello` firmware). Exposed as
`edge_nodes[]` in `GET /api/v1/discover` alongside BLE `candidates[]`.

### Layer 3 — Intake: claim (TOFU) → register secret → relocate

PWA adds a **"Standby / unassigned hardware"** group (`node_id · chip · sensors · fw ·
last-seen`). Selecting one opens **"assign to area"**, which drives:

1. **Claim (TOFU-lock).** The dictator sends an enroll request to `home/edge/<node>/enroll/req`.
   The node accepts an **unsigned** enroll request **only if not yet claimed** (its own
   one-time TOFU) and replies on `home/edge/<node>/enroll/reply` with its NVS secret
   (directed topic, cleared after read — the secret is transmitted once).
2. **Register.** The dictator records `{node_id, base_mac, secret}` in the LUT under a
   **TOFU-lock**: the first claim of a `node_id` binds it; any later claim of the same
   `node_id` with a **different secret/MAC is rejected** (anti-hijack — this, not value
   collision, is the "fake node" guard). The node flips `hello.enrolled=true`.
3. **Relocate.** `device_relocate <node> → <area>` wakes the node's `air_quality` ability
   and mints the `gas_<area>` device record — **not** the MAC-based `POST /api/v1/devices`.

---

## Trust model & security once-over

- **Secret handover is symmetric** (HMAC-SHA256, unchanged) so the key must cross the wire
  **once** at intake. On the airgapped/home LAN with the anonymous broker this is an
  accepted **TOFU** (Hugh: "TOFU sounds fine"). Hardening for an internet-facing broker
  (a shared bootstrap key authenticating the handover) is a later change that does **not**
  alter this model.
- **Node-side TOFU:** a node accepts exactly one unsigned enroll; after that every command
  requires its secret. Re-adoption requires an explicit NVS wipe (physical presence).
- **Dictator-side TOFU-lock:** first `(node_id)` claim wins; mismatched re-claims rejected.
- `hello` is unauthenticated (like `status`/`adv`) but carries only identity and drives no
  action itself — a spoofed `hello` can at most add a candidate an admin must still act on.
- Intake is a VIP-gated admin control-plane op; relocate already restarts the ingest fleet
  (known device-admin gotcha) — no new exposure.

## Consequences / open

- `hello` and any `enroll/reply` are retained/directed — relocating or retiring a node must
  clear them (empty retained publish) so stale candidates/secrets don't linger; the TTL
  cache also ages `hello` out.
- **Demo vehicle:** `standby_c6` is *already enrolled* (secret in node + LUT), so it proves
  the **discover → relocate** loop today. The **self-provision + register** path (Layer 0/1
  claim) is proven separately on a unit flashed with the generic, secret-less image.
- Bring-up hazard (recorded): the C6 build tree's `secrets.h` is per-node and git-ignored;
  for a *legacy* reflash use `enroll_node.py --from-manifest --reuse` for the **correct**
  node_id or you overwrite a live node's identity (split-brain).
- First tryout is `.210`-dev-contained (node brokers to `mqtt://192.168.0.210:1883`); prod
  (ha-2) rollout is a later, separate step.
