# PWA-driven firmware loading — design plan

**Status:** Draft / plan. No decision recorded yet — this doc exists so the *sequence* can be picked
deliberately. Graduates to an ADR once Hugh chooses the transport order (§6).

**Directive origin:** Hugh, 2026-07-31: *"plan the implementation of PWA-driven firmware loading onto
MCUs (S3, C6, etc) with options to load e.g. gas sensors, etc as part of the firmware load."*

**Builds on:** ADR-0020 (shared edge/panel firmware core + identity gate), ADR-0018 (node provisioning
record-keeping), ADR-0034 (Node / Ability / Entity object model), ADR-0036 (standby edge-node intake),
ADR-0010/0011 (physical-presence trust root).

**Prior framing:** the PWA surface maturity map already calls device onboarding the *"giant dragon"* —
custom-firmware flash is the last un-automated step between "hardware in hand" and "device in a room".

---

## 1. What the ask decomposes into

"Load firmware from the PWA, with options" is really three separable problems:

1. **Choice** — the user picks a board and a set of abilities (gas sensor type, BLE relay, GATT history,
   LED, display…). Something must turn that choice into a concrete, bootable configuration.
2. **Transport** — how the bits physically reach the chip (§5). This is the fork that needs a decision.
3. **Identity** — the node must end up with a `node_id` and a per-node HMAC `cmd_secret`, recorded in
   `instance/node_secrets.enc`, without ever letting an image built for one node land on another.

(1) and (3) are mostly *already solved* or nearly so. (2) is the real work.

---

## 2. Inventory — what already exists

More than half the machinery is in place. Verified 2026-07-31 against the tree.

| Piece | Where | State |
|---|---|---|
| Shared component core (29 components) | `firmware/components/` | ADR-0020, live |
| Per-target builds | `edge/esp32c3`, `edge/esp32c6`, `edge/esp32s3-eth`, d1001 panel | live |
| Module × build matrix (drift-guarded) | `edge/MATRIX.md`, `tools/gen_module_matrix.py`, `tests/test_module_matrix.py` | live |
| **Gas driver selection is already RUNTIME** | `ha_gas_start(node, HA_GAS_SENSOR_{SGP40,SGP30,BME680}, sda, scl)` | live |
| I2C bus probe (all three chips at distinct addresses) | `ha_gas.c` `gas_bus_scan()` — SGP40 `0x59`, SGP30 `0x58`, BME680 `0x76/0x77` | live |
| NVS config overlay (namespace `"ha"`) | `ha_config.c` — `broker_uri`, `wifi_ssid`, `wifi_psk`, `ntp_server`, `ota_host` | live |
| Repoint boot-count revert (trial-boot + rollback) | `ha_config.c` `RP_NS` namespace | live |
| Node enrolment **over the API** | `POST /api/v1/nodes` → `handle_enroll_node` — mints `cmd_secret` + mqtt creds, atomically re-encrypts the LUT with verified round-trip decrypt and `.bak` rollback | live |
| Enrolment CLI, manifest-driven | `tools/enroll_node.py --from-manifest --reuse` | live |
| Per-node manifest (identity + wiring) | `edge/*/nodes.yaml` — `mac`, `target`, `sensor`, `area`, `broker`, `ota_host` | live |
| Signed A/B OTA w/ self-test + auto-rollback | `tools/edge_ota.py`, `ha_ota.c` — host-pinned, sha256-gated | live |
| Long-job escalation out of the API sandbox | `ha-admin-job.path` → `ha-admin-job.service`; poll `GET /api/v1/devices/jobs/{id}` | live |
| Node self-announce → adopt-to-room | ADR-0036: `home/edge/<node>/hello` → PWA "Standby hardware" → relocate | live |
| A/B partition layout w/ 16 KB NVS | `edge/*/partitions.csv` — `nvs @0x9000 0x4000`, `ota_0/ota_1` 1.75 MB each, **448 KB reserved** at `0x390000` | live |

**Correction to a stale comment:** `edge/esp32c6/nodes.yaml` describes `sensor:` as a *"compile-time
select (HA_GAS_SGP30)"*. That is no longer true of the driver layer — `ha_gas.c` says so explicitly
(*"Runtime config resolved in ha_gas_start (was compile-time #ifdef in the old per-node forks)"*), and
**all three drivers are linked into every image** ("the drivers are small"). Only `app_main` still
resolves the choice from a `secrets.h` `#define`. That single `#if` chain is the last compile-time tie.

---

## 3. The central obstacle — per-node image branding

Today **an image is branded for exactly one `node_id` and one gas chip**. This is deliberate and was
earned: it exists because on 2026-07-05 a `coffice_c6 + SGP40` image was OTA'd onto `cbed_c6`. The gate
is checked by `enroll_node`/OTA against `nodes.yaml`.

That model is fundamentally incompatible with "pick options in the PWA and flash", because every
combination of (target × sensor × abilities × node) would need its own build. It does not scale, and it
puts an ESP-IDF build on the request path.

### Proposal: generic image + identity/config in NVS

- **One reproducible, hash-pinned image per _target_** (c6, s3-eth, c3, d1001), built ahead of time.
- **Identity and config move to an NVS blob**, minted per-node at flash time: `node_id`, `cmd_secret`,
  `broker_uri`, `ota_host`, wifi creds, `gas_sensor`, enabled abilities. `ha_config` already overlays
  five of these from NVS namespace `"ha"` — this extends the existing mechanism rather than inventing one.
- **The anti-cross-flash invariant is preserved, not dropped — and gets stronger.** The blob is minted
  against the **eFuse MAC read off the chip on the cable at flash time**, and `app_main` refuses to run
  if `esp_efuse_mac() != nvs.mac`. The guarantee moves from *"which `.bin` did you pick"* to *"which NVS
  blob"*, and the MAC now comes from the physical chip instead of being trusted from a manifest entry a
  human typed. The 2026-07-05 incident could not recur: an image is no longer *for* a node at all.

### Gas sensor: three levels of "choice", pick per-node

Because the addresses are distinct and `gas_bus_scan()` already probes them, the PWA's "which gas
sensor" option can be:

1. **Auto** (recommended default) — firmware probes I2C at boot, picks the driver that ACKs, reports
   what it found in its `hello`. Zero configuration; a re-solder is picked up on reboot.
2. **Pinned** — NVS `gas_sensor` key forces one driver. Use when two candidates are on the bus, or to
   assert intent so a *missing* sensor is an error rather than a silent fallback.
3. **None** — gas lane disabled; node is BLE-relay only.

`ha_gas` is already no-op-safe when the sensor is absent (it logs over MQTT and returns; the node keeps
relaying), so all three modes degrade correctly.

---

## 4. Phases

**Phase 0 — de-brand the image** *(firmware; the enabling step, and small)*
- Replace `app_main`'s `#if defined(HA_GAS_SGP30)/#elif HA_GAS_BME680` chain with NVS lookup → auto-probe
  fallback.
- Extend the `ha_config` `"ha"` NVS overlay with `node_id`, `cmd_secret`, `gas_sensor`, `abilities`.
- Add the eFuse-MAC bind check in `app_main`, before MQTT starts.
- Keep `secrets.h` working as the *fallback* when NVS is empty, so the existing fleet is unaffected and
  this can ship without touching a single deployed node.
- Regenerate `edge/MATRIX.md` (`tools/gen_module_matrix.py --write`); `tests/test_module_matrix.py`
  pins it.

**Phase 1 — transport** (§5, needs the decision)

**Phase 2 — PWA surface**
Grow the ADR-0036 "Standby hardware" panel into a **Node** view: target (auto-detected from the chip),
abilities checklist, gas sensor (auto-probed, override-able), room, display name. Post → job → progress
→ node announces `hello` → adopt-to-room. The tail of that flow already works end-to-end.
Reuse the canonical-rooms dropdown (`GET /api/v1/rooms`) — free-text area fields spawn phantom areas.

**Phase 3 — build service**
A `ha-firmware-build` job producing hash-pinned per-target images plus a manifest the PWA reads, so the
UI can never offer an image that does not exist. Keeps ESP-IDF entirely off the request path.

---

## 5. The three transports

### (a) OTA-only — reflash already-enrolled nodes
- **Mechanism:** PWA → admin job → `tools/edge_ota.py`. Entirely existing machinery.
- **Covers:** changing abilities / gas-sensor pinning / firmware version on a **live** node.
- **Does not cover:** first flash of new hardware (an un-enrolled node has no `cmd_secret`, so it rejects
  every signed command including OTA — `HA_CMD_SECRET ""` rejects all).
- **Cost:** days. **Risk:** low — signed, host-pinned, sha256-gated, A/B with self-test + auto-rollback.
- **Gotcha:** C6 fleet OTA must originate from ha-2 (node `ota_host` pin), and a fresh node may reject
  all OTAs on a dangling `node_id` — that fix cannot ship over the air.

### (b) Server-side USB flash — board plugged into `.210` / ha-2
- **Mechanism:** PWA → admin job → `esptool.py` on `/dev/ttyACM0`. Reuses `ha-admin-job.path`'s existing
  privilege escalation (the API sandbox is `NoNewPrivileges` and cannot do this itself).
- **Covers:** first flash, including minting the NVS identity blob.
- **Cost:** moderate. **Risk:** low-moderate — needs device-node access, a serial-port lock (one flash at
  a time), and a real power-cycle after flashing before serial/new firmware works.
- **Security:** the `cmd_secret` never leaves the box. **This is the strongest posture of the three.**
- **Constraint:** the board must be physically at the box. Given the trust root is *physical presence at
  the dictator* (ADR-0010/0011), that constraint is arguably a feature, not a limitation.

### (c) WebSerial + esptool-js in the PWA
- **Mechanism:** browser talks to the board over USB directly via the WebSerial API.
- **Covers:** everything, from anywhere, with no box involvement — the real dragon-slayer.
- **Cost:** highest. **Risks / constraints:**
  - Chromium-family only (no Firefox/Safari WebSerial). Requires a **secure context** — the PWA is
    already HTTPS, but the self-signed cert must be *trusted* on the client, not click-through.
  - **Air-gap: esptool-js must be vendored offline.** No CDN. The Artifact/PWA CSP and the air-gapped
    network both forbid external fetches.
  - Requires a user gesture per port grant.
  - **The `cmd_secret` transits the browser.** Today it only ever exists at build time in `secrets.h`
    and in the encrypted LUT.

---

## 6. Security once-over

*(Standing directive: every feature gets a time-boxed security pass.)*

The one material change in posture is **where the `cmd_secret` lives during provisioning**:

| Path | Secret exposure |
|---|---|
| Today (`secrets.h` at build) | Build host only |
| (b) server-side flash | Box only — **no change in posture** |
| (c) WebSerial | Server → browser JS → USB. New exposure surface. |

Mitigations for (c), in increasing order of strength:
1. Ship the secret to the browser only over the authenticated admin session, one-shot, never persisted
   to `localStorage`/IndexedDB.
2. Mint the secret *after* flash: the node boots generic, announces, and is issued its secret over a
   short-lived provisioning channel.
3. **Node-born secret / TOFU** — the node generates its own keypair at first boot and the server only
   ever sees the public half. **This is exactly the Phase-2 work ADR-0036 deferred.** It removes the
   exposure entirely and is the right long-term answer.

**Recommendation:** if (c) is wanted, do the ADR-0036 node-born-secret work *first*, or accept (b) as
the first-flash path and let (c) handle only non-secret reconfiguration. Do not ship (c) with a
plaintext `cmd_secret` crossing the browser as an interim.

Other checks: the eFuse-MAC bind (§3) preserves the ADR-0020 anti-cross-provisioning guarantee; the
admin-job path is already `require_admin`-gated; OTA remains signed + host-pinned + hash-verified
regardless of which transport wrote the image.

---

## 7. Open questions for Hugh

1. **Transport sequence** — §5. Recommendation: **(a) → (b) → (c)**. (a) ships on existing machinery in
   days and immediately delivers "change a node's abilities from the PWA"; (b) closes first-flash with
   the job queue we already have; (c) is the eventual dragon-slayer and wants the node-born-secret work
   in front of it.
2. **Is (c) actually wanted**, given (b) satisfies the physical-presence trust root by construction?
3. **Gas-sensor default** — auto-probe, or require an explicit pin so a missing sensor is a loud error?
4. **Does `nodes.yaml` survive** as the identity source of truth, or does the registry become
   authoritative once identity lives in NVS? (Note `edge/*/nodes.yaml` on ha-2 is already known-stale;
   ADR-0036's registry cross-check works around it.)
5. **Panels (d1001/E1001)** — in scope for PWA flashing, or edge nodes only for now? The panel build has
   its own recovery bootloader (ADR-0030) and a "don't deep-sleep a panel under iteration" constraint.

---

## 8. Dead ends / notes for whoever picks this up

- Do **not** plan on building per-(node × sensor) images on demand. ESP-IDF builds are minutes long and
  the combinatorics are hostile; §3 exists specifically to avoid this.
- Do **not** drop the identity gate to make flashing easier. Move it (eFuse-MAC bind), don't remove it —
  it was earned by a real cross-flash incident.
- The repo `venv/` is shared and ESPHome pins `paho<2`, which breaks `edge_ota`/repoint tooling; any
  build/flash service must not assume one venv satisfies both.
- Intake must **un-hide `autohome_airgap`** during provisioning (ESPHome won't reliably join a hidden
  SSID) and re-hide after.
