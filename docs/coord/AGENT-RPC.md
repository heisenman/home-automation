# Agent-to-Agent RPC — task coordination over the cluster bus

**Status:** Proposed v0.1 by `ops` (2026-06-24), awaiting `dev` ack. Adopt by reading this + using
`tools/agents/coord.py`. No GitHub needed at runtime — this rides the existing out-of-band bus.

## Who
Two Claude instances coordinate here. Stable ids:
- **`ops`** — the desktop / `.245`-standby-side operator (failover, ops, server-side glue).
- **`dev`** — the on-device builder on **210** (firmware, server features, ADRs).

(`anon` is allowed for read-only `list`/`ready`/`agents`.)

## Where
- **Broker:** the dictator **VIP `192.168.0.200:1883`** (anonymous, LAN). VIP-addressed so the ledger
  follows the dictator on failover. `ops` reaches it over the LAN; `dev` reaches it as localhost.
- **Namespace:** `ha/agents/#` — deliberately **separate from `ha/cluster/#`** (which drives
  keepalived/heartbeat sensing) so agent chatter can never perturb failover logic. Not bridged; it
  doesn't need to be — both agents use the one VIP broker as common ground.
- **Durability:** mosquitto `persistence true` on 210 → retained task state survives broker restarts.

## Data model (retained messages = source of truth)
- `ha/agents/tasks/<id>` → one retained JSON record per task:
  ```json
  {"id":"adr15-phase0-vip-repoint","title":"Repoint edge nodes + clients to VIP .200",
   "owner":"","status":"open","deps":["adr15-finalize"],
   "created_by":"dev","created_ts":0,"updated_ts":0,"updated_by":"dev","note":""}
  ```
- `ha/agents/agent/<id>` → retained beacon: `{agent,last_active,current,note}` (auto-updated on every
  mutating command — a cheap "what's the other agent doing / is it alive" signal).

### States
`open → claimed → in_progress → done`, with `blocked` (recoverable) and `cancelled` (terminal) off to the
side; `release` returns an owned task to `open`. **Terminal:** `done`, `cancelled`.

### Readiness & serialization (the point of all this)
A task is **READY** ⇔ `status==open` **AND every `dep` is `done`**. Agents only `claim` ready tasks.
When you finish work, `done <id>` flips it and the tool prints any **dependents that just became ready** —
so dependent work serializes automatically across the two agents without anyone watching in real time.

### Claim safety
`claim` does read-check-write, then re-reads after a short settle to detect a race. Deterministic
tiebreak on a true tie: **the lexicographically-smaller agent id wins** (`dev` < `ops`), the other yields.
`--force` overrides ownership/readiness guards (use sparingly, say why in `--note`).

## The convention (this is the actual ask)
1. **End of every task/turn:** call `done` (or `block`/`release`) on what you touched. That is the
   "RPC signal at end of task." It updates retained state + your beacon.
2. **Start of every turn:** `ready` (what can I pick up?) and `mine` (what am I mid-flight on?) and
   `list` (whole board). That is "review pending action items."
3. **Taking work:** `claim <id>` then `start <id>`. Never work an item owned by the other agent.
4. **New work for either of us:** `add <id> --title … [--deps a,b]`. Encode dependencies so order is
   enforced by the graph, not by memory.

Because neither agent polls continuously (we run only when invoked), this is an **asynchronous dead-drop**:
messages wait on the bus until the other agent's next turn. Hugh may still relay "go look" to shorten the
loop, but the ledger — not memory or chat — is the shared truth.

## Tool
`tools/agents/coord.py` (stdlib + `mosquitto_pub/sub`; no jq/paho). Examples:
```bash
export HA_AGENT_ID=ops          # dev exports HA_AGENT_ID=dev
python3 tools/agents/coord.py list
python3 tools/agents/coord.py ready
python3 tools/agents/coord.py add my-task --title "…" --deps adr15-finalize
python3 tools/agents/coord.py claim my-task && python3 tools/agents/coord.py start my-task
python3 tools/agents/coord.py done my-task --note "shipped in <commit>"
python3 tools/agents/coord.py agents      # liveness/what's-the-other-doing
```
Broker override: `--broker` or `$HA_COORD_BROKER`. Identity: `--as` or `$HA_AGENT_ID`.

## dev: how to accept / amend
Adopt as-is by claiming + completing `coord-protocol-ack` (seeded on the board). To amend, edit this file
+ `coord.py`, push, and `note` the ack task with what changed. Open for counter-proposal — that's the
"you two figure it out" part; v0.1 is a starting point, not a decree.
