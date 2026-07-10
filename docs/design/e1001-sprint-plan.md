# E1001 sprint plan — full intake as a production device (dev-owned)

> **Ownership:** `dev`, handed from `ops` by Hugh **2026-07-10**. Supersedes the "E1001 stays ops until
> go-ahead" line (memory `dev-ops-ownership-line`). ESPHome reTerminal E1001 = the c-office wall panel
> (`e1001-c-office`), EFuse MAC `A4:CB:8F:CF:46:E8`, now on the **air-gap net → ha-2** (production dictator).
>
> **Sources folded in:** `docs/coord/E1001-5C-REPOINT-HANDOFF.md` (dev→ops→back-to-dev), the ops board task
> `e1001-finish-features` (firmware/deploy COMPLETE), `instance/e1001-c-office-enrollment-2026-07-09/README.md`,
> `docs/design/e1001-gap-register.md`, and this session's quarantine catch (ADR-0032).

## Verified current state (live, 2026-07-10)
- **Firmware DONE + deployed:** ops shipped Steps 0–3 (ADR-0024 safety policy, PCF85063T RTC holdover,
  MLT-8530 buzzer), renderer polish, paginated fetch, safe_mode boot-loop recovery (6s, deep-sleep-safe,
  wake-cycle validated). Renamed `e1001-bench → e1001-c-office`, area `c_office`. All Hugh-confirmed.
- **Live:** esphome 2026.6.4 / ESP-IDF 5.5.4, uptime ~3.4h, `reset_reason = esphome.ota` (clean), the
  `api: reboot_timeout: 0s` fix (commit 6e29d94) is **holding** (no ~15-min reboot loop). `sleep_default:false`
  (dev-awake so OTA stays reachable until physical mount).
- **THE GAP (this session):** the panel's onboard SHT40 publishes `home/edge/e1001-c-office/sht40/adv`
  (`mac:"e1001-c-office-sht40"`) but is **NOT in ha-2's `devices.yaml`** → it was **silently dropped** from the
  panel repoint until the quarantine deploy. Now captured as `edge/E1001-C-OFFICE-SHT40` in
  `ha-2:instance/db/quarantine.db` (preserved, accumulating). Verified: `load_registry` uppercases keys
  (`{mac.upper(): …}`), so the lowercase enrollment key matches the uppercase quarantine identity — no bug.
- **5c repoint NOT done:** E1001 still points its broker at host `192.168.1.210`, not the VIP `192.168.1.200`
  (only 1 device is on the `.200` listener today) — a keepalived failover would drop the panel.

## Sprint goal
Land E1001 as a **fully-integrated, self-sufficient production device on ha-2**: close the data gap (enroll
SHT40 + recover the quarantine backlog), finish the 5c broker repoint so failover protects it, clear/park the
residual bench items, and hand the physical bits back to Hugh — no silent data loss, no failover blind spot.

---

## Phase 0 — Take ownership + baseline (no gate)
- [ ] Claim on the board: `coord.py --as dev add e1001-sprint … && claim && start`; mark `e1001-finish-features`
      residuals as folded-in (or leave to ops as the 2 bench items — see Phase 3).
- [ ] Stand up / confirm an **isolated esphome build env** (NOT the shared `venv/` — esphome pins
      `paho-mqtt<2` and silently breaks every `ha-*` service on next restart; `docs/coord/SHARED-VENV-ISOLATION.md`).
- [ ] Snapshot: E1001 broker IP on ha-2, quarantine accrual (`tools/quarantine.py inspect edge E1001-C-OFFICE-SHT40`),
      current `ha-2:instance/devices.yaml`.

## Phase 1 — Close the data gap  ✅ **DONE 2026-07-10** (Hugh: record)
Executed: SHT40 enrolled on ha-2 `devices.yaml` (`e1001_c_office`, c_office; registry 18→19), edge-mapper
reloaded, `home/c_office/e1001_c_office/state` live + accumulating in hot.db (ingest-stamped ts — panel is
clockless, sends `ts:""`). Quarantine backlog merged (recovered the 04:20:09 reading), quarantine clean,
stale retained bench topics cleared. **Bug found+fixed en route** (commit `1db37eb`): a blank `ts` collapsed a
clockless device's quarantine backlog to 1 row via `COALESCE` — normalized blank→NULL (+test), redeployed both
systems. Original detail below.

### (original plan) ⭐ highest value, additive, dev-only  *(one Hugh decision)*
The SHT40 is real telemetry being quarantined right now. Enroll it on **ha-2** (production; the enrollment
README targeted `.210` pre-repoint — the target moved to ha-2 with the air-gap cutover).
- [ ] **DECISION (Hugh): record or purge the SHT40?** It's redundant with `meter_pro_c_office` (SwitchBot
      temp/RH already in c_office). Recommend **record** (redundancy is a design goal; `data-storage-is-primary`)
      — but Hugh reserved this call ("record-or-purge is part of the intake call"). Everything below assumes record.
- [ ] Enroll on `ha-2:instance/devices.yaml` (mirror `<type>_<area>` pattern; keep `capabilities`):
      ```yaml
        "e1001-c-office-sht40":
          device_id: "e1001_c_office"
          device_type: "sht40"
          area: "c_office"
          capabilities: [temperature, humidity]
      ```
- [ ] Restart ha-2 `ha-edge-mapper` (reload registry). Confirm `home/c_office/e1001_c_office/state` publishes
      and lands in ha-2 `hot.db` (fresh `device_last_seen`).
- [ ] **Recover the backlog:** `tools/quarantine.py merge edge E1001-C-OFFICE-SHT40 --device-id e1001_c_office
      --area c_office --device-type sht40` (WITHOUT `--register` — the row is added above with proper
      capabilities). Replays the captured window into hot.db losslessly, marks rows merged.
      *(Note: readings from the repoint until the quarantine deploy were dropped BEFORE the net existed = lost;
      quarantine holds everything since. This recovers the captured span, no more loss going forward.)*
- [ ] Clear stale retained edge topics from the old bench identity (empty retained publish):
      `home/edge/e1001/event`, `e1001-bench/#` diag (cosmetic). On the ha-2 broker.
- [ ] *(optional, dev-convenience)* mirror the SHT40 row into the `.210 ~/ha-airgap-standby` devices.yaml so the
      warm standby stays a faithful mirror.

## Phase 2 — 5c broker repoint  ✅ **DONE 2026-07-10**
Executed: `e1001.yaml` broker `.1.210 → VIP .1.200` (commit `24e335f`; bff/ntp kept at `.1.210`,
`reboot_timeout:0s` preserved). Built via the **esphome docker image** on `.210` (`ghcr.io/esphome/esphome`
— installed docker box-local; inherently isolated from the shared venv, which stayed paho-v2 intact).
Confirmed `.200` baked into generated `main.cpp`, OTA'd to `192.168.1.131` (7.5s, "OTA successful"). Verified:
E1001 reconnected on ha-2's **`.200` VIP listener**, uptime climbed 6→126s past the mark-valid/rollback
window (the D1001-class rollback did NOT bite), SHT40 kept logging (21 distinct-ts rows). Panel now
failover-protected. **New capability:** `.210` can now build/OTA the E1001 (docker esphome) — see
`provisioning/reterminal/e1001/BUILD.md`.

### (original plan) *(ESPHome reflash; window advised)*
- [ ] Edit `provisioning/reterminal/e1001/e1001.yaml`: `broker:` (~L226) `192.168.1.210 → 192.168.1.200` (VIP).
      **Leave** `bff_base_url` (~L16) + `ntp_server` (~L18) at `.210` — dev is doing **broker-only float**
      fleet-wide (standby doesn't serve BFF/NTP on the VIP yet).
- [ ] ⚠️ **PRESERVE `api: reboot_timeout: 0s`** — do not let the reflash regress the reboot fix ([[esphome-api-reboot-timeout]]).
- [ ] ⚠️ Build in the **isolated esphome env**, OTA `e1001-c-office` from `.210` (which straddles the air-gap net).
- [ ] Verify: E1001's IP appears on `ha-2` `.200:1883` listener (`ss -tnH '( sport = :1883 )' | grep .200`);
      uptime climbs past ~15 min; still publishes `e1001-c-office/#`; SHT40 still flows to hot.db.
- [ ] ⚠️ **OTA risk:** `ota-verify-window-rollback` — D1001 rolled back on first OTA attempt (mark-valid gated on
      slow-boot MQTT connect); retry stuck. E1001 may do the same → do in a window, be ready to re-OTA.

## Phase 3 — Residual bench items  *(Hugh-gated, low-pri, need physical bench + USB)*
> **DECISION 2026-07-10 (Hugh):** **Step 6 (PDM mic) DROPPED** — not wanted. Step 4 offset + Step 5
> power-budget remain **deferred low-pri** (both need physical hardware; the offset also needs a mid-SoC cell —
> battery is near-full now).
Parked with the physical device; not blocking production integration. Either keep on ops's `e1001-finish-features`
or migrate here:
- [ ] **Step 4** — USB/charging battery offset RE-run (mid-SoC + USB-unplug). e-ink = no display offset; USB+charging
      only. Prior run exists; Hugh = low-pri.
- [ ] **Step 7** — power-budget bench (real idle/wake draw + duty-cycle ledger) + onboard **mic** bring-up.

## Phase 4 — Finalize + hand physical back to Hugh
> **DECISION 2026-07-10 (Hugh):** **Sleep Enable DROPPED** — keep the panel **awake / OTA-reachable** (deep-sleep
> only connects during brief wake windows → kills easy OTA; not worth it for a still-iterated device). Panel runs
> always-on. Physical **mount stays optional/Hugh** and does NOT affect OTA (still on Wi-Fi, no sleep).
- [ ] **Hugh:** physical mount, then flip **Sleep Enable** over MQTT (moves `sleep_default` effective → true;
      the panel becomes a wake-cycle deep-sleep node). Confirm wake→fetch→render→resleep on the wall.
- [ ] Docs: mark `E1001-5C-REPOINT-HANDOFF.md` resolved, close the enrollment README, update the gap register /
      roadmap; retire redundant handoff docs. Board: `done` the sprint task; reconcile `e1001-finish-features`.

---

## Gates & decisions summary
| Item | Owner | Gate |
|---|---|---|
| SHT40 record-vs-purge | **Hugh** | decide before Phase 1 merge (recommend record) |
| SHT40 enrollment + backlog merge | dev | none (additive) |
| 5c broker repoint OTA | dev | do in a window; OTA-rollback risk, reversible |
| Bench offset / power / mic | dev+Hugh | physical bench access + Hugh (low-pri) |
| Physical mount + Sleep Enable | **Hugh** | physical |

## Standing gotchas (bake in)
- **Isolated esphome venv** — never build esphome in the shared `venv/` ([[shared-venv-esphome-paho]]).
- **Preserve `reboot_timeout: 0s`** across any reflash.
- **ha-2 is air-gapped** — `.210` straddles both nets; OTA + registry edits happen against ha-2 from `.210`.
  `instance/devices.yaml` is per-box + gitignored (real MACs) — edit ha-2's copy.
- **ESPHome fleet reboot-timeout sweep** (Levoit prime suspect) is a *separate* FOLLOWUP — E1001 already has the fix.
