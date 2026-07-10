# Agent-to-Agent RPC — task coordination over the cluster bus

> This board is the **control plane** in a two-plane model. For big content / fast same-host chatter /
> working-tree safety, use the **data plane** (`~/.claude/host/bin/coord-local.py`). See
> [HOST-COORD.md](HOST-COORD.md) and `~/.claude/host/HANDBOOK.md`. Rule: the board holds a *reference*,
> `coord-local` holds the *content*. Two dev seats now exist: `dev` and `dev2`.

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
- **Durability:** mosquitto `persistence true` on 210 → retained task state survives broker *restarts*.
  **Failover is ephemeral by decision (Hugh, 2026-06-24):** `ha/agents/#` is NOT bridged, so a dictator
  *swap* resets the board; whoever notices re-seeds it. Coordination state is transient, so this is fine —
  we did not add a second bridged namespace just to persist it across failover.

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

**Cancelled deps don't deadlock:** a dep stuck in `cancelled` can never become `done`, so its dependents
would block forever. They're not silently stuck — `list` flags them `STUCK: dep cancelled (…)` with an
escape hint, and `dep <id> --remove <dep>` (or `--add`) edits the dependency cleanly without `--force`.

**Gated tasks (human GO):** `add … --gate hugh` (or `gate <id> --set hugh`) marks a task as needing a human
GO. A gated task is held out of `ready` and refuses `claim`/`start` (shows `GATED` in `list`). **Only Hugh
clears a gate** — `gate <id> --clear` — agents must never self-clear (POLICY.md). This encodes "needs Hugh"
in the board itself, so `ready` means *actually actionable by an agent*, not just unblocked-on-deps.

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
python3 tools/agents/coord.py dep my-task --remove some-dep   # edit deps (escape a cancelled/wrong dep)
python3 tools/agents/coord.py agents      # liveness/what's-the-other-doing
```
Broker override: `--broker` or `$HA_COORD_BROKER`. Identity: `--as` or `$HA_AGENT_ID`.

## Reaching a peer — TWO delivery paths (use `summon`)
There are two ways to deliver to a peer, and picking the wrong one hides the message. **Prefer `summon`,**
which chooses correctly — **visible-first** (Hugh 2026-07-10 prefers *seeing* coordination, not background):
```
tools/agents/summon.py <target> "<msg>" [--task ID] [--headless] [--dry-run]
```
1. **VISIBLE — inject into a live interactive session (DEFAULT).** If the target has a registered LIVE
   interactive session (pid alive + a `/dev/pts` tty in the coord-local roster), `summon` (and the
   `board-watch` cron on a task assignment) uses **`tty-inject.py`** (TIOCSTI, auto-submit) to type the message
   into that session's terminal — the peer's chat **gets a new turn, no keypress**. Requires `sudo` NOPASSWD
   for `tty-inject.py` (`/etc/sudoers.d/tty-inject`; sudo-hardening broke this once — 2026-07-10).
2. **HEADLESS fallback — `coord.py wake`.** With `--headless`, on inject failure, or when the target has **no
   live interactive session**, `summon` falls back to a NON-retained `ha/agents/wake/<target>`; the
   `ha-agent-wake@<id>` watcher (`tools/agents/wake/watch.sh`, debounce+cooldown) invokes a **headless one-shot
   `claude -p` runner** (POLICY.md-scoped, fresh context — memory+board+git, not chat history).

**Correction (2026-07-10):** an earlier version of this doc said *"this interactive session can never be
externally woken."* That is only true of the **wake path** (2, spawns a *separate* headless agent). The
**tty-inject path (1) DOES reach a live interactive session** — the real "message a registered chat" mechanism,
and what `board-watch` uses to deliver work-orders. PID **and tty** registration is the delivery address —
keep registration current.

- **Hard constraint (headless path):** a wake watcher only runs on a box with the `claude` CLI — **today only
  210**. Kill switch: `systemctl stop ha-agent-wake@<id>` (idle cost is zero regardless).

## dev: how to accept / amend
Adopt as-is by claiming + completing `coord-protocol-ack` (seeded on the board). To amend, edit this file
+ `coord.py`, push, and `note` the ack task with what changed. Open for counter-proposal — that's the
"you two figure it out" part; v0.1 is a starting point, not a decree.
