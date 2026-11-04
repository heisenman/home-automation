# tools/ — operator tooling

Scripts the operator/agent runs against the fleet. Python + a few shell. Not services (those are `../systemd/`).

*↑ The by-location node for `tools/` in the [root AGENTS.md](../AGENTS.md) tree (ADR-0021/0025); the by-capability index is [`docs/REUSE.md`](../docs/REUSE.md). Link up, don't duplicate.*

## The task board (start every task here)

`agents/coord.py` — two-Claude coordination over an MQTT task ledger (`ha/agents/#` on VIP `.200`).
- `python3 tools/agents/coord.py --as <ops|dev> list|ready|mine` — see the board.
- `... add <id> --title "…" --note "…"` / `... note <id> --note "…"` / `... done <id>` — update it.
- `... show <id>` — full task record **incl. its step log** (the "what did the agent actually do" view).
- `... wake dev` — delegate a **bounded** task to a fresh headless `claude -p` on `.210` (POLICY-bounded;
  NOT Hugh's interactive dev chat). Convention: **ops** = desktop/.245-side, **dev** = .210.

### Delegation contract — a delegated worker LOGS ITS STEPS

`note` is last-write-wins (a snapshot); `log` **accumulates** (a journal). Any agent that claims a delegated
task MUST narrate each major step via `... log <id> --step "…"` — e.g. `pull ✓ @<sha>`, `build ✓ (<target>,
<fwver>)`, `OTA sent`, `validated: <observed>`. This turns the board from a coarse status snapshot into a
step-by-step record of HOW the task ran, visible after the fact with `show <id>` and live via
`mosquitto_sub -t 'ha/agents/#'` (the retained task record re-publishes on every append). When **posting** a
delegated task, restate this in the task note so the worker adopts it. Terminal `done`/`block` still carries a
full summary note.

## Frequently used

| Tool | Purpose |
|------|---------|
| `node_bringup.py` | Gated one-shot edge-node bring-up (FIRMWARE-GUIDE §7 as a tool) |
| `enroll_node.py`, `edge_sign.py` | Enroll / mint a node's command secret + emit its per-device `secrets.h` (never hand-write secrets). `--from-manifest` fills board wiring; `--reuse` re-emits an enrolled node from its existing secret (rebuild path, no re-mint). |
| `edge_nodes.py` | Loader for the per-node manifest `edge/esp32c6/nodes.yaml` (node_id→{mac,target,sensor,area,broker,ota_host}) — the cross-provisioning source of truth (ADR-0020), consumed by `enroll_node`/`node_bringup` + the OTA/flash identity gate. |
| `edge_ota.py`, `edge_pull_history.py`, `edge_gatt.py` | OTA / GATT history / GATT ops against a node |
| `ha2_deploy.sh <commit> <svc…>` | deploy a commit's changed files to ha-2: VIP inhibit (`.maintenance-fit`, auto-cleared) → one tar stream → md5-verify → restart services (one multiplexed ssh) |
| `team_deploy.sh push\|pwa <c>\|d1001 <c> <panel>` | deploy gateway for Catherine's account: `sudo -u visko tools/team_deploy.sh …` pushes, deploys PWA/BFF-only commits to ha-2 (ha-api + ha-api-tls), or builds + OTAs ONE D1001 panel and waits for the new build with display on; posts the result to the host board as `deploy` (sudoers `/etc/sudoers.d/ha-team-deploy`) |
| `mint_panel_token.py` | Panel operator token (reTerminal) |
| `aranet_history.py`, `import_*_csv.py` | History backfill/import |
| `gen_tls.py`, `gen_vapid.py` | Cert / key generation |
| `power_report.py`, `mesh_probe.py`, `gap_watcher.py` | Diagnostics |
| `aq_experiment_report.py` (+ `.sh`) | Daily AQ v2 shadow-experiment report — runs on ha-2 read-only, filed on .210 under `instance/aq-experiment/reports/` by `ha-aq-experiment-report.timer` (SENSOR-METHODOLOGY §9.5) |
| `dehum_readiness.py` (+ `_report.sh`) | Dehumidifier-study readiness gates (G2a/G2b/P3) + last-24 h stats — piped to ha-2 read-only, filed daily on .210 under `instance/dehum-study/reports/` (status.json + history) by visko crontab 14:41 UTC (study doc §6c). Also `dehum_audit.py` (Phase 0), `dehum_phase1.py` (Phase 1) |
| `dehum_drift.py` (+ `_report.sh`) | Dehumidifier health: condition-compensated drift alert (refrigerant leak / airflow fault) on compressor power, pauses/h and system L/kWh vs a frozen per-unit baseline — scores only runs inside the baseline's conditions and low leverage, CUSUM, two indicators to alert. Bundled with `dehum_readiness.py` as a library and piped to ha-2; per-run history kept on .210 in `instance/dehum-drift/runs.jsonl` (1-min rungs live 7 d); crontab 14:45 UTC (study doc §7) |
| `fan_block.py` (+ `.sh`) | Dehumidifier study fan-only zero blocks — blower ON 10:00/15:00 PT for 30 min via the control-API press path, skips if Aprilaire/heat/person's fan setting, hands off to the follow coupling; cron on .210 until the HVAC zero passes (study doc §6c) |
| `furnace_closet_aq.py` | Mech-closet AQ vs furnace heat calls (A heat / B heat-while-blowing / C blower-only), read-only on ha-2; daily via `ha2_report.sh` → `instance/furnace-aq/reports/` (SENSOR-METHODOLOGY §10) |
| `ha2_report.sh` | Generic: pipe a read-only stdlib script to ha-2 and file its markdown on .210 under `instance/<name>/reports/` |
| `host-leds.sh` | Box operability LEDs |
| `gen_module_matrix.py` | Generate/`--check` the firmware device×module matrix (`edge/MATRIX.md`) from each build's `CMakeLists` (ADR-0020 drift-guard; pinned by `tests/test_module_matrix.py`) |

## Gotchas

- Serial `/dev/tty*` on the desktop need `chmod 666` (user not in `dialout`; standing chore:
  `sudo usermod -aG dialout $USER`).
- `mosquitto_sub` piped block-buffers → `timeout` kills before flush. Pipe through `stdbuf -oL` and use
  `-C <n>` (clean-exit after n msgs) to capture output reliably.
