# Resume / open action items — 2026-07-26 (failover + intake session)

Pick-up notes so open work can resume seamlessly. Tracked as board/tasks #8, #9, #10.
Session context in memory: `failover-blind-to-service-completeness`, `keep-ha2-current-not-frozen`,
`airgap-ssh-port-47222`, `dehum-graceful-cant-repower`, `peer-repair-request-scoped`.

## Shipped this session (done, on ha-2 + main)
- Self-healing watchdog + peer-repair + screamer + completeness signals (`bbab7be`) — LIVE on ha-2.
- Service-aware failover ACTIVATED + drill-proven: keepalived weight −40→−60 (ha-2 `chk_dictator`,
  .210 `chk_dictator_airgap`); `.unfit` marker → VIP hands to standby → reclaims. Standby un-stalled
  (was 11h behind: ha-2 `cluster.env` missing `CLUSTER_SSH_PORT=47222` + drifted `reconcile-history.sh` +
  narrow reconcile-agent allowlist; all fixed `8b03c8c`).
- ha-2 reconciled to `main`: 14 drifted files deployed (server code already matched).
- Dehum recovered (was stuck OFF — see task #8). PWA: area dropdowns (`d6b4a08`) + rename/display-name
  on adopt (`598c797`,`b895597`).

## Open action items

### #8 — Graceful controller can't re-power an off device  (small, isolated)
`server/control/controller.py` graceful-mode path only sets MODE (run_mode/idle_mode), never power. If the
Midea is externally powered off (`running=false`) it can't recover it → issues mode changes → `status=mismatch`,
stays off (house not dehumidified). **Fix:** ON path sends `running=on` when it reads `running=false`, THEN
sets run_mode=continuous. Add a controller test. Memory: `dehum-graceful-cant-repower`. Manual recovery used:
`drv.set(running="on", mode=2)` (device on airgap `.1.119`).

### #9 — Code-drift detector for ha-2  (structural win)
A `.210`-side checker: md5 tracked files vs ha-2 over SSH, alert `kind=code_drift` (reuse ntfy+PWA-banner
plumbing). Would have caught the stale `reconcile-history.sh`. Working manual method (use it as the seed):
```
FILES=$(git ls-files server tools failover systemd provisioning | grep -vE '^tests/|\.md$|^docs/')
echo "$FILES" | xargs md5sum | sort -k2 > /tmp/local.md5
echo "$FILES" | sed 's|^|/home/visko/home_automation/|' | ssh ha-2 'xargs md5sum' \
  | sed 's| /home/visko/home_automation/| |;s|/home/visko/home_automation/||' | sort -k2 > /tmp/ha2.md5
join -j2 /tmp/local.md5 /tmp/ha2.md5 | awk '$2!=$3'
```
NOTE the ABSOLUTE-path trick (sed prefix) — sidesteps the "forgot cd on the remote" bug. Exclude per-box
`instance/` + box-specific `/etc/systemd/system` env. Daily timer + on-demand + post-deploy verify.
Helper on `.210`: `~/.local/bin/ha2` / `ha2cp` (bake in the repo cd + base IP .1.210).

### #10 — migrate-vs-fresh choice in device rename/adopt  (Hugh's design; frontend + backend)
Today `apply_rename_worksheet` is **migrate-always** (moves hot.db+parquet+rungs+registry to the new id/area).
For adopting a STANDBY into a new identity that's usually wrong (want a FRESH start); a real physical move
wants migrate. Must be an explicit, required choice.
- **PWA** (`server/web/app.js`, DeviceCard "Advanced" `<details>` ~line 1254): MOVE the room/area relocate
  (`relArea` select, currently a separate Entity-plane block ~line 1233) INTO the Advanced section next to the
  id-rename. Add a REQUIRED radio group "migrate history" vs "start fresh"; DISABLE all Advanced action buttons
  (rename Preview/Apply, relocate) until one is chosen. Thread the chosen mode into both calls.
- **Backend** (`server/api/control.py` `handle_device_rename`/`handle_device_relocate` +
  `server/maintenance/apply_rename_worksheet.py`): honor `data_mode` — `migrate` (current) or `fresh` (register
  new id/area, do NOT carry old history). Add tests.
- Touches the FLEET-RESTARTING rename path → build + test carefully, deploy ha-2 under `.maintenance-fit`
  VIP-inhibit, verify a real rename both ways. Not blocking (polish). Raised after renaming `gas_standby`.

## Deploy discipline (directive `keep-ha2-current-not-frozen`)
Commit → scp + md5-verify to ha-2 → restart affected services under `.maintenance-fit` (VIP held) → verify.
Reach ha-2 at base IP `.1.210` (the VIP `.1.200` moves). Cross-box SSH uses port **47222** only.
