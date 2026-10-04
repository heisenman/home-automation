# PWA-driven Levoit flashing — design note

**Status:** Accepted 2026-10-04 (Hugh: *"start at the beginning and do it all"*, after the plan below was put to
him). Extends [pwa-firmware-loading.md](pwa-firmware-loading.md) (transport (b): server-side USB on `.210`) to
an **ESPHome appliance**. No new ADR: the intake half is already ADR-0036 (amendment 2026-10-01), and this
changes *how the bits reach the chip*, not any contract.

**Ask:** program a new Levoit Vital 200S from **Add device** in the PWA, instead of the by-hand recipe in
[provisioning/levoit/README.md](../../provisioning/levoit/README.md) (which units 1–3 used).

## 1. What already existed (reuse-first)

| Piece | Where | Reused as |
|---|---|---|
| Flash panel + job (survey → POST → poll steps) | `server/web/app.js` "Flash new hardware", `/api/v1/flash*`, `admin_job` op `flash` | same panel, same job queue, new `kind` |
| Port lock, port listing, chip detect, edge-manifest MAC lookup | `server/maintenance/edge_flash.py` | imported, not copied |
| ESPHome purifier discovery + adopt | `server/ingest/esphome_discovery.py`, `server/esphome_intake.py` | unchanged — a generic unit is just another ESPHome name |
| Shared purifier body | `provisioning/levoit/levoit-vital200s-c3.common.yaml` | unchanged; the generic unit is one more thin file |

So the new code is only what is genuinely Levoit-specific: a generic image, an OEM backup, and a flash
sequence that survives the manual download-mode dance. It lives in **its own module**
(`server/maintenance/levoit_flash.py`), not in `edge_flash.py`, because the two flows share a transport but
nothing else: an edge node gets a generic IDF image plus an NVS identity blob and a node-born secret; a Levoit
gets one ESPHome factory image and has no secret at all (it is a server-driven actuator).

## 2. Decisions

1. **One generic image, identity from silicon.** `provisioning/levoit/levoit-generic.yaml` sets
   `name_add_mac_suffix: true`, so each unit names itself `levoit-<last 6 hex of its MAC>` at boot. ESPHome
   derives the MQTT topic prefix and the fallback-AP SSID from that runtime name (verified in the ESPHome
   2026.6 source: `MQTTClientComponent::set_topic_prefix`, `WiFiComponent` AP default). The flasher therefore
   **never compiles**: the old flow's 4-minute per-unit Docker build happens once, ahead of time
   (`tools/levoit_build_generic.sh`). Room and device_id come from PWA adopt, exactly as before.
   *Rejected:* compiling per unit inside the flash job. That puts Docker, internet access (the `tuct/levoit`
   component is fetched from GitHub) and a 4-minute build on the request path.
2. **One shared OTA key for generic units** (`ota_password_generic`). This is the cost of decision 1, and Hugh
   accepted it. The units sit on the air-gapped network and the key stays in the gitignored `secrets.yaml`.
   The three hand-named units keep their per-unit keys and files. Nothing about them changes.
3. **The OEM backup is part of the flash and gates it.** Before erasing, the job reads the full 4 MB to
   `instance/oem-backups/levoit-<mac6>-oem-<date>.bin` (off-git, plus a `.sha256` sidecar). If the read fails,
   is the wrong size, or is blank (all `0xFF`), **nothing is erased**. If a backup for that MAC already
   exists, it is reused rather than re-read: a re-flash of an already-converted unit must not overwrite the
   real OEM image with an ESPHome one.
4. **Every new connection checks first and asks the operator only if it has to** (Hugh, 2026-10-04: *"the
   code checks connection and pings the user if needed at the beginning of any new communication with the
   uC"*). CP210x RTS auto-reset failed on 2 of 3 units. So `_connect` tries `no-reset` (the chip is already in
   download mode), then `default-reset` (RTS, which needs nobody on units where it works). If both fail, it
   posts a prompt to the job record (the PWA shows *"hold IO0, tap EN"*) and keeps retrying for up to 3 min,
   then carries on by itself. Pulsing RTS is safe whenever a connection opens, because nothing has been erased
   yet. The backup and the write are **separate connections**: the backup's stub is left at the flash baud,
   where a fresh sync can't reach it. So expect **one re-pulse between them** (Hugh: *"backup and re-flash won't
   be a single step"*). A unit already backed up needs no second connection. The write connection re-reads the
   MAC and refuses if a different chip answered.
   *Rejected:* one esptool session for everything (it relied on the link surviving a 2-min read and a
   re-sync), and two operator buttons (more clicks, and it still needs the same connection check).
5. **The operator names the kind; the chip can't.** A Levoit's ESP32-C3 looks the same as our own bare
   `esp32c3` edge board. The panel asks *"What is this? Edge node | Levoit Vital 200S"*. Guards:
   - the chip must be an ESP32-C3 with 4 MB flash;
   - a MAC already in an edge `nodes.yaml` is refused (that board is one of ours, not a purifier);
   - the backup is checked for VeSync/Levoit markers, and the result is shown as a hint rather than enforced
     (a unit already on ESPHome has none).
6. **Network is baked into the image, not chosen.** The generic image carries the same coherent pair as every
   Levoit: `autohome_airgap` (preferred), household fallback, and broker `.1.200`. There is no network picker
   for this kind, so the broker/Wi-Fi mismatch that `edge_flash.apply_defaults` exists to prevent can't occur.

## 3. Flow

```
Add device → Flash new hardware → Scan USB → "Levoit Vital 200S"
  → panel: "hold IO0→GND, tap EN→GND (IO0 can be let go after), then Flash"
  → job: connect* → ESP32-C3 + 4 MB? → MAC not an edge node?
         → OEM backup (skipped if one is on file) → connect* again (expect "re-pulse EN") → same MAC?
         → erase → write generic factory image → hash-verified
     * connect = no-reset → default-reset → else prompt the operator in the panel and keep retrying (3 min)
  → panel: "release IO0, unplug the programmer, reassemble, plug into mains"
  → unit boots as levoit-xxxxxx → appears in Standby hardware ONLY once on mains (no PM2.5 on programmer power)
  → Adopt into a room (existing ADR-0036 ESPHome intake) → pick automation source
```

Still physical, and staying that way: opening the unit, wiring the header, the IO0/EN gesture, and the power
cycle.

## 4. Security once-over

- **No new secret path.** The generic image embeds the Wi-Fi PSKs and the shared OTA key, just as the
  per-unit images already do. It lives in the gitignored ESPHome build tree on `.210` and is never returned by
  the API (the survey reports only path, hash and build time).
- **The OEM backup may hold VeSync cloud credentials.** It is written under `instance/` (gitignored) at mode
  0640 and never served.
- **Flash stays `require_admin` + `.210` USB**, and the cable is the physical-presence trust root (ADR-0010/0011).
- The edge-manifest MAC refusal stops the purifier image overwriting one of our own nodes by mistake.

## 5. Verification

Host tests: `tests/test_levoit_flash.py`, which mocks esptool and covers the backup gating, the backup reuse,
the edge-MAC and wrong-chip refusals, the missing-image report, the predicted name, the operator prompt
(raised once, then cleared), auto-reset units needing no prompt, the timeout, and a swapped board. The **live proof needs
the next physical Levoit**: flash it from the PWA, confirm `levoit-<mac6>/status online` on `192.168.1.200`,
then on mains confirm it appears in Standby hardware and adopt it (CONFORMANCE §B R3 per-unit range check as
usual). Recheck recipe: [runbook-device-verification.md](../runbook-device-verification.md).

## 6. Dead ends / gotchas

- `wifi.ap.ssid` in the shared body is per-unit text. On a generic image it must be **removed** (`!remove`),
  not blanked; ESPHome then defaults it to the runtime name.
- A unit on programmer power publishes fan/filter state but **no PM2.5**, so discovery's purifier signature
  (fan + PM2.5) does not match until it is on mains. This is correct behaviour.
- Don't rely on the esptool CLI for this. Every CLI call ends in a hard reset, and with RTS unreliable that
  means a re-pulse per call. The Python API keeps control of when a new connection happens.
