# Host-level coordination (pointer)

The generic "how co-resident Claude instances coordinate on this computer" framework does **not** live
in this repo — it's host-level and project-agnostic, so it lives under the machine's Claude home:

- **Handbook:** `~/.claude/host/HANDBOOK.md`
- **Data-plane tool:** `~/.claude/host/bin/coord-local.py` (state under `~/.claude/host/coord/`)
- **Discovery:** `~/.claude/CLAUDE.md` points there, loaded every session on this box.

## How this project fits the model

The framework is a **two-plane** split:

- **Control plane** — small, serialized, cross-machine task state. *This project supplies it:* the MQTT
  task board `tools/agents/coord.py` (see [AGENT-RPC.md](AGENT-RPC.md)). It carries the task ledger,
  dependency graph, readiness, and cross-machine liveness. It stays — it's good at small serialized
  state and reaches instances on other machines (ops, wake-runners).
- **Data plane** — big handoffs, fast chatter, "editing X now", working-tree safety. *The host tool
  supplies it:* `coord-local.py`, same-host and file-based.

**Rule:** the board holds a *reference*; `coord-local` holds the *content*. This is the frictionless
version of the standing "push content to git/files, post a short reference to the board" practice.

### Two dev seats
`coord.py` now recognizes `dev` **and** `dev2` (`KNOWN_AGENTS`) — the two co-resident dev fronts on 210
(ops is mostly retired). Each front uses a distinct seat so board beacons and task claims don't collide.
