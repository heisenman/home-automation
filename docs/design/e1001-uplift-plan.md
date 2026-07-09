I'm# E1001 uplift plan — applying the D1001 learnings (2026-07-09, ops)

Consolidates the E1001's remaining work into one prioritized plan, folding in the items that appeared
after the gap register (2026-07-05) and **explicitly applying the D1001 session's learnings**. The
step-by-step detail for the feature backlog still lives in **`e1001-gap-register.md`** (the audit +
Steps 0–7) and **`e1001-roadmap.md`**; this doc is the *sequencing + new-work + learnings* layer.

## Where the E1001 is (grounded, not from memory)
- **Framework:** ESPHome on the **esp-idf** framework, `esp32-s3-devkitc-1`, e-paper. `flash_size: 8MB`
  (chip is 32 MB W25Q256 — >16 MB OTA needs the esp-idf experimental flag; matters for a golden partition).
- **Live on `.71`:** gap-register **Steps 1–3 shipped** — ADR-0024 safety policy (boot-gate + warn),
  PCF85063T RTC holdover, MLT-8530 buzzer. Blockers **B1** (no firmware hard power-off — schematic sheet 4)
  and **B2** (no burst-telemetry knob) are RESOLVED.
- **Buttons:** GPIO3(green)/4/5. GPIO3 = wake + **OTA-escape-hatch** (held at wake → holds OTA, blocks
  sleep). ⚠️ GPIO3 is wired `input: pullup, inverted` = **active-LOW** — the *opposite* of the D1001's
  verified active-HIGH. **Do not port the D1001 polarity; verify the E1001's on-device.**
- **Wifi/broker are COMPILE-TIME** (`!secret wifi_ssid`, `broker: 192.168.0.210`). No runtime overlay.

## D1001 learnings applied here (the point of this pass)
**Method (non-negotiable — these cost us real time on the D1001 when skipped):**
- **DOCS-FIRST, and do NOT port D1001 assumptions.** The GPIO3 polarity is already proof: D1001 active-HIGH,
  E1001 active-LOW. Re-verify every hardware fact on the E1001 (button polarity, flash size, buffer limits,
  power rails) from its schematic/config, not by analogy. See [[feedback-docs-first]] + [[feedback-dont-overrun-proof]].
- **Capture-to-file, examine after.** E1001 build/OTA is the **esphome DOCKER** image (no host CLI) →
  [[reference-e1001-build-toolchain]]; diag on MQTT `e1001-bench/#`. Never trust a live read of a moment.
- **Decompose before code; prove-don't-assert; bench-verify each chunk** before moving on. The D1001 repoint
  bench caught a real shared-component NULL panic — hardware proof finds what review doesn't.
- **Board protocol:** short board message → substance in an `instance/` file over sshfs; 210 work → dev via
  the board, never hand Hugh 210 commands.

**Technical patterns — what ports vs what does NOT:**
- **Repoint / air-gap:** the D1001's `ha_config` runtime NVS overlay + signed `op:repoint` **does NOT port** —
  ESPHome bakes wifi/broker at compile time. E1001 repoint = **recompile with the new net + OTA** via
  `provisioning/esphome-repoint.sh` (DJ-19); dev's `device_push` ESPHome class drives it. Simpler mechanism,
  but it needs a build, not a runtime command.
- **ADR-0030 recovery-to-golden:** the bootloader-override + **immutable golden `test` partition** pattern
  (proven on D1001) *can* port — ESPHome is esp-idf underneath, so a `bootloader_components/` override is
  possible — **but** (a) it needs the **32 MB flash enabled** (currently 8 MB) for a golden slot, (b) GPIO3
  polarity differs, (c) the ESPHome build wraps the partition table, so the golden slot + override must be
  injected through ESPHome's esp-idf hooks. The existing **OTA-escape-hatch (held-button → OTA hold)** is the
  interim recovery; full golden-recovery is Phase 2 and the heaviest item — treat as its own project.
- **Renderer resourcing lesson:** the D1001 map flicker (tearing down the whole screen every tick) →
  "**don't redraw what didn't change.**" For **e-paper** this is even sharper: full refresh is slow + flashy
  by nature, so the renderer polish (Step 5) must lean on **partial refresh** and minimize full-screen
  refreshes. Carry the "static layer vs value layer" instinct over.

## Prioritized workstreams
**P0 — fetch-buffer bug (URGENT; the panel is functionally down).** Board `e1001-fetch-buffer-bug` (dev2):
`/api/v1/sensors` grew to ~14.3 KB (16→21 sensors) and exceeds the firmware's `http_request` response buffer
→ body captured = 0 bytes → **0 tiles**. Device is otherwise healthy (wifi, BLE relay). Two fixes, do both:
(a) **ops:** raise `max_response_buffer_size` in `e1001.yaml` `http_request:` + OTA — immediate unblock; (b)
**dev:** a **curated/bounded** panel endpoint so it can't outgrow the buffer as the fleet scales (the durable
fix). (a) is a one-line firmware change + docker OTA; verify tiles return on `.71`.

**P1 — finish features (gap-register Steps 4–7).** Mostly gated on Hugh/bench, no new design:
- **Step 4 — USB/charging offset run** (bench: mid-SoC cell + USB unplug; `battery_characterize.py measure_offsets`
  → re-push profile, no reflash). e-ink has no display-load offset (already 0).
- **Step 5 — renderer polish + hardening:** spec-driven metric count + `°` glyph; **partial-refresh discipline
  (D1001 lesson)**; gate/remove bench affordances behind a dev flag.
- **Step 6 — deploy prep (gated → Hugh):** rename `e1001-bench`→`e1001-<area>` (needs the **AREA**); gated
  `devices.yaml` SHT40 row + enrollment; mount/power; enable deep-sleep + validate wake.
- **Step 7 — power-budget bench** (meter, Hugh+bench) → then mic.

**P2 — air-gap repoint (panel-airgap-repoint Chunk 3, ops).** ESPHome rebuild+OTA path
(`esphome-repoint.sh`), coordinated with dev's `device_push` ESPHome class. Migration is LIVE, so this is
real now — but simpler than the D1001 (no firmware repoint op to write; it's build-config + OTA). Sequence
after P0 (a broken panel shouldn't be migrated).

**P3 — ADR-0030 golden-partition recovery port: RETIRED (Hugh, 2026-07-09).** Redundant for the E1001
architecture and not worth the cost. The E1001's recovery is already covered by three cheaper mechanisms:
(1) **ESPHome `safe_mode`** — boot-loop detection → auto-fallback to the last-good OTA slot (live @8b1b4d7,
`boot_is_good_after:6s`, validated on-device); (2) **archived known-good image** off-device
(`instance/e1001-known-good-archive-2026-07-09/`, factory+ota bins); (3) **~5-min USB reflash** (the panel is
physically grabbable — proven this session). A golden partition would need the 32 MB experimental-OTA flag +
`bootloader_components/` surgery through ESPHome's esp-idf layer for near-zero added benefit. Not doing it.

## Sequencing
1. **P0 fetch-buffer** (unblock the panel) — this week.
2. **P1 Steps 4–5** (offset run + renderer/hardening) — bench + ops.
3. **P1 Step 6 deploy** — gated on Hugh giving the AREA.
4. **P2 repoint** — with dev, once the panel is healthy + deployed.
5. **P1 Step 7** power/mic — later, its own focused project. (**P3 golden-recovery RETIRED** — see above.)

## Open gates for Hugh
- A **bench window** (Step 4 offsets + Step 7 power meter + mic).
- (AREA settled = c_office; P3 golden-recovery retired.)
