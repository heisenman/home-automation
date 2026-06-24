# Cutover — promote ha-dev (192.168.0.210) to dictator, demote .245 to Aranet edge-relay

**Two Claude sessions coordinate through THIS file — git is the message bus; Hugh holds the GO on the
gated steps.** `245-side` = desktop Claude (CIFS + SSH to both boxes). `210-side` = on-device Claude.

> **HARD INVARIANT: never two active `ha-controller`s (Midea split-brain).** A brief *zero* is fine.
> On the handoff: `.245` controller STOPPED and confirmed → **then** `210` controller starts. No overlap, ever.

---

## STATUS — edit ONLY your own line, commit, push. `git pull` before EVERY action.
```
245-side : phase 0 / ready / <ts>
210-side : phase 0 / ready / <ts>
GO gates (Hugh):  G1 stop-245-controller = [ ]   G2 start-210-controller = [ ]   G3 demote-245 = [ ]
Midea snapshot (pre-cutover):  state = <on/off>   target = <%>   control-RH source = <id>
```

## Roles
- **245-side (desktop Claude):** pre-flight on `.245`; add the Aranet bridge; stop `.245` controller;
  demote `.245` to relay; verify/rollback the `.245` side.
- **210-side (on-device Claude):** confirm `210` is dictator-ready (Midea control config + LAN reach);
  start `210` controller; verify control + the relayed Aranet on `210`.
- **Hugh:** sets the GO gates in STATUS; physically watches the dehumidifier through the handoff.

## Coordination rules
1. `git pull` immediately before any step.
2. Only ONE side acts at a time — phases are serialized; within a phase follow the step owners.
3. Update your STATUS line + check your step boxes, then commit+push so the other side sees it.
4. Do NOT cross a ⛔ gate until Hugh has checked it in STATUS.
5. If anything looks wrong: STOP, use the phase Rollback, don't improvise across boxes.

---

## Phase 0 — PRE-FLIGHT (read-only; no changes). Both sides verify + report in STATUS.
- [ ] **0a [210]** Dictator-ready: `instance/control.yaml` + control secrets present; can resolve/reach the
  Midea on the LAN (dry-run, NO command issued); all `ha-*` services healthy; 10 meters live;
  **`ha-controller` still OFF.**
- [ ] **0b [245]** `.245` is the SOLE active `ha-controller`. It is publishing the Aranet —
  `mosquitto_sub -h localhost -t 'home/+/aranet_radon/state' -v` returns readings; **note the exact topic
  (area slug)** and `.245`'s mosquitto drop-in dir. Inventory `.245`'s `ha-*` services.
- [ ] **0c [Hugh + both]** Snapshot the Midea's current state into STATUS (to confirm continuity through the
  handoff). Agree the handoff window.

## Phase 1 — Aranet relay (additive · reversible · independent of control — DO THIS FIRST)
Per `edge/aranet-245-relay.md`. Get the relay working+verified before the risky handoff to de-risk it.
- [ ] **1a [245 · Hugh present]** Add the `bridge-ha-dev.conf` drop-in on `.245` (out-only, single topic
  `home/<area>/aranet_radon/state`), then `sudo systemctl restart mosquitto` on `.245`. One brief broker
  blip; `.245` services auto-reconnect within seconds.
- [ ] **1b [210]** Verify on `210`: `mosquitto_sub -h localhost -t 'home/<area>/aranet_radon/state' -v` shows
  relayed readings; `/api/v1/sensors` count **10 → 11**, `aranet_radon` appears.
- **Rollback:** delete the drop-in, `sudo systemctl restart mosquitto` on `.245`.

## Phase 2 — Dictator handoff (split-brain-critical) ⛔ STRICT ORDER + Hugh GO
- [ ] **2a [245] ⛔ requires G1** Stop + disable `.245` `ha-controller`. Confirm inactive.
  Update STATUS → `245 controller STOPPED`.
- [ ] **2b [210] ⛔ requires G2 — and only after 2a shows STOPPED in STATUS** Enable + start `210`
  `ha-controller`. It is now the SOLE dictator.
- [ ] **2c [Hugh + both]** Verify exactly ONE controller active (`210`); it reads sensors, makes a correct
  Midea decision, the unit responds; `.245` issues nothing. Confirm against the 0c snapshot.
- **Rollback (reverse order):** stop `210` controller → re-enable/start `.245` controller. Back to `.245` dictator.

## Phase 3 — Demote .245 to Aranet edge-relay ⛔ requires G3
- [ ] **3a [245]** Stop/disable `.245`'s now-redundant dictator services (e.g. `ha-api`/dashboard).
  **KEEP RUNNING:** `ha-scanner` (decodes the Aranet) + `mosquitto` (with the bridge). Confirm `.245` now
  only scans + relays the Aranet.
- [ ] **3b [both]** Final check: `210` = dictator with **11 sensors** (10 local + Aranet via bridge) + Midea
  control; `.245` = Aranet relay only. Reconcile `docs/FOLLOWUPS.md` per `docs/CHECKPOINT.md`.

---

## Notes
- **Scanner fix `ec8511d` is NOT a prerequisite** for the bridge — `.245` already decodes + publishes the
  Aranet topic and the bridge just forwards it verbatim. It's a correctness cleanup (right company ID
  `0x0702`). Apply it to `.245` only if pre-flight **0b** shows `.245` has stopped decoding the Aranet.
- **No remote broker creds** in the bridge (210's broker is anonymous). If 210 gains auth later, add
  `remote_username` / `remote_password` to the drop-in.
- **Midea control config on 210 is the make-or-break pre-flight (0a):** if 210 can't actually drive the
  dehumidifier, do NOT stop `.245`'s controller — fix 0a first, or we get a no-dictator gap on the Midea.
- **When `.245` is fully decommissioned** the bridge retires; the Aranet then needs an in-range source
  feeding 210 directly — the ESP32-C6 Wi-Fi relay (`edge/esp32c6/dev-box-relay.md`).
