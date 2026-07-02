# tools/ — operator tooling

Scripts the operator/agent runs against the fleet. Python + a few shell. Not services (those are `../systemd/`).

## The task board (start every task here)

`agents/coord.py` — two-Claude coordination over an MQTT task ledger (`ha/agents/#` on VIP `.200`).
- `python3 tools/agents/coord.py --as <ops|dev> list|ready|mine` — see the board.
- `... add <id> --title "…" --note "…"` / `... note <id> --note "…"` / `... done <id>` — update it.
- `... wake dev` — delegate a **bounded** task to a fresh headless `claude -p` on `.210` (POLICY-bounded;
  NOT Hugh's interactive dev chat). Convention: **ops** = desktop/.245-side, **dev** = .210.

## Frequently used

| Tool | Purpose |
|------|---------|
| `node_bringup.py` | Gated one-shot edge-node bring-up (FIRMWARE-GUIDE §7 as a tool) |
| `enroll_node.py`, `edge_sign.py` | Enroll a node / mint its command secret (never hand-write secrets) |
| `edge_ota.py`, `edge_pull_history.py`, `edge_gatt.py` | OTA / GATT history / GATT ops against a node |
| `mint_panel_token.py` | Panel operator token (reTerminal) |
| `aranet_history.py`, `import_*_csv.py` | History backfill/import |
| `gen_tls.py`, `gen_vapid.py` | Cert / key generation |
| `power_report.py`, `mesh_probe.py`, `gap_watcher.py` | Diagnostics |
| `host-leds.sh` | Box operability LEDs |

## Gotchas

- Serial `/dev/tty*` on the desktop need `chmod 666` (user not in `dialout`; standing chore:
  `sudo usermod -aG dialout $USER`).
- `mosquitto_sub` piped block-buffers → `timeout` kills before flush. Pipe through `stdbuf -oL` and use
  `-C <n>` (clean-exit after n msgs) to capture output reliably.
