#!/usr/bin/env python3
"""Agent-to-agent coordination over the cluster bus (no GitHub needed).

A tiny, durable task ledger shared by the two Claude instances ("ops" on the desktop/.245 side,
"dev" on 210). State lives as RETAINED MQTT messages under `ha/agents/#` on the dictator VIP broker
(192.168.0.200, anon) — persistence is ON, so it survives broker restarts. This is deliberately OFF
the `ha/cluster/#` failover namespace so it can never confuse keepalived/heartbeat sensing.

Protocol (see docs/coord/AGENT-RPC.md):
  - One retained topic per task:  ha/agents/tasks/<id>  -> JSON task record (source of truth).
  - One retained beacon per agent: ha/agents/agent/<id> -> {last_active, current, note}.
  - A task is READY when status==open AND every dep is status==done. Agents only claim ready tasks.
  - End-of-turn convention: call `done`/`block`/`release` so the OTHER agent, on its next turn,
    sees the change via `list`/`ready` and any dependents serialize automatically.

Transport is mosquitto_pub/sub (present on both boxes); JSON is stdlib only (no jq/paho needed).
Identity: --as <id> or $HA_AGENT_ID  (ops | dev). Broker: --broker or $HA_COORD_BROKER (default VIP).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

BROKER = os.environ.get("HA_COORD_BROKER", "192.168.0.200")
PORT = int(os.environ.get("HA_COORD_PORT", "1883"))
BASE = "ha/agents"
TASKS = f"{BASE}/tasks"
AGENTS = f"{BASE}/agent"
STATES = ("open", "claimed", "in_progress", "blocked", "done", "cancelled")
ACTIVE = ("claimed", "in_progress", "blocked")   # owned-and-not-finished
KNOWN_AGENTS = ("ops", "dev")


def now() -> int:
    return int(time.time())


def _pub(topic: str, payload, retain: bool = True) -> None:
    args = ["mosquitto_pub", "-h", BROKER, "-p", str(PORT), "-t", topic]
    if retain:
        args.append("-r")
    if payload is None:                      # clear a retained topic
        args.append("-n")
    else:
        args += ["-m", json.dumps(payload, separators=(",", ":"))]
    subprocess.run(args, check=True, timeout=10)


def _read(topic: str, wait: float = 2.0):
    """Return the dict at a single retained topic, or None if absent/unparseable."""
    try:
        r = subprocess.run(["mosquitto_sub", "-h", BROKER, "-p", str(PORT), "-t", topic,
                            "-C", "1", "-W", str(int(wait))],
                           capture_output=True, text=True, timeout=wait + 3)
    except subprocess.TimeoutExpired:
        return None
    out = r.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out.splitlines()[0])
    except json.JSONDecodeError:
        return None


def _read_tree(prefix: str, wait: float = 2.0) -> dict:
    """Return {id: record} for every retained task/beacon under a prefix."""
    try:
        r = subprocess.run(["mosquitto_sub", "-h", BROKER, "-p", str(PORT),
                            "-t", f"{prefix}/#", "-v", "-W", str(int(wait))],
                           capture_output=True, text=True, timeout=wait + 3)
    except subprocess.TimeoutExpired as e:
        r = e                                  # -W makes sub exit; timeout is the stop signal
    out = getattr(r, "stdout", "") or ""
    items = {}
    for line in out.splitlines():
        topic, _, body = line.partition(" ")
        if not body:
            continue
        try:
            rec = json.loads(body)
        except json.JSONDecodeError:
            continue
        items[topic.rsplit("/", 1)[-1]] = rec
    return items


def _all_tasks() -> dict:
    return _read_tree(TASKS)


def _is_done(tid: str, tasks: dict) -> bool:
    t = tasks.get(tid)
    return bool(t) and t.get("status") == "done"


def _ready(t: dict, tasks: dict) -> bool:
    return t.get("status") == "open" and all(_is_done(d, tasks) for d in t.get("deps", []))


def _cancelled_deps(t: dict, tasks: dict) -> list:
    """Deps that are CANCELLED — they can never become 'done', so they'd block this task forever
    unless edited out. Surfaced in `list` with an escape hint (fixes dev's deadlock critique)."""
    return [d for d in t.get("deps", []) if (tasks.get(d) or {}).get("status") == "cancelled"]


def _beacon(agent: str, current: str = "", note: str = "") -> None:
    _pub(f"{AGENTS}/{agent}", {"agent": agent, "last_active": now(),
                               "current": current, "note": note})


def _save(t: dict, agent: str) -> None:
    t["updated_ts"] = now()
    t["updated_by"] = agent
    _pub(f"{TASKS}/{t['id']}", t)


def _get(tid: str) -> dict | None:
    return _read(f"{TASKS}/{tid}")


def _fmt(t: dict) -> str:
    deps = ",".join(t.get("deps", [])) or "-"
    owner = t.get("owner") or "-"
    return f"  [{t.get('status','?'):<11}] {t['id']:<28} owner={owner:<5} deps={deps:<22} {t.get('title','')}"


# ---- commands -------------------------------------------------------------

def cmd_list(a, agent):
    tasks = _all_tasks()
    if not tasks:
        print("(no tasks)"); return
    order = {s: i for i, s in enumerate(("in_progress", "claimed", "blocked", "open", "done", "cancelled"))}
    for t in sorted(tasks.values(), key=lambda x: (order.get(x.get("status"), 9), x["id"])):
        if a.all or t.get("status") not in ("done", "cancelled"):
            cd = _cancelled_deps(t, tasks) if t.get("status") not in ("done", "cancelled") else []
            if cd:
                tag = f"  <-- STUCK: dep cancelled ({','.join(cd)}); escape: dep {t['id']} --remove {cd[0]}"
            else:
                tag = "  <-- READY" if _ready(t, tasks) else ""
            print(_fmt(t) + tag)


def cmd_ready(a, agent):
    tasks = _all_tasks()
    rs = [t for t in tasks.values() if _ready(t, tasks)]
    if not rs:
        print("(nothing ready — all open tasks are blocked on deps, or none exist)"); return
    print("READY to claim:")
    for t in sorted(rs, key=lambda x: x["id"]):
        print(_fmt(t))


def cmd_mine(a, agent):
    tasks = _all_tasks()
    mine = [t for t in tasks.values() if t.get("owner") == agent and t.get("status") in ACTIVE]
    print(f"owned by {agent} (active):" if mine else f"(no active tasks owned by {agent})")
    for t in sorted(mine, key=lambda x: x["id"]):
        print(_fmt(t))


def cmd_agents(a, agent):
    for b in sorted(_read_tree(AGENTS).values(), key=lambda x: x.get("agent", "")):
        age = now() - int(b.get("last_active", 0))
        print(f"  {b.get('agent','?'):<5} last_active={age}s ago  current={b.get('current') or '-'}  {b.get('note','')}")


def cmd_add(a, agent):
    if _get(a.id):
        print(f"ERROR: task '{a.id}' already exists"); sys.exit(1)
    t = {"id": a.id, "title": a.title, "owner": "", "status": "open",
         "deps": [d for d in (a.deps.split(",") if a.deps else []) if d],
         "created_by": agent, "created_ts": now(), "updated_ts": now(),
         "updated_by": agent, "note": a.note or ""}
    _save(t, agent); _beacon(agent, note=f"added {a.id}")
    print(f"added: {a.id}"); print(_fmt(t))


def cmd_claim(a, agent):
    t = _get(a.id)
    if not t:
        print(f"ERROR: no such task '{a.id}'"); sys.exit(1)
    tasks = _all_tasks()
    if not a.force and not _ready(t, tasks):
        unmet = [d for d in t.get("deps", []) if not _is_done(d, tasks)]
        print(f"REFUSED: '{a.id}' not ready (status={t['status']}, unmet deps={unmet or '-'}). Use --force to override.")
        sys.exit(2)
    if t.get("owner") and t["owner"] != agent:
        print(f"REFUSED: '{a.id}' already owned by {t['owner']}"); sys.exit(2)
    t["owner"] = agent; t["status"] = "claimed"
    _save(t, agent)
    time.sleep(0.4)                            # settle, then verify we still hold it (race tiebreak)
    cur = _get(a.id) or t
    if cur.get("owner") != agent:
        # someone raced us. Deterministic tiebreak: lexicographically-smaller agent id wins.
        if agent > cur.get("owner", ""):
            print(f"CONFLICT: lost claim race to {cur['owner']} (it sorts earlier) — yielding"); sys.exit(3)
        t["owner"] = agent; t["status"] = "claimed"; _save(t, agent)
        print(f"CONFLICT resolved in our favor (we sort earlier) — reclaimed {a.id}")
    _beacon(agent, current=a.id, note=f"claimed {a.id}")
    print(f"CLAIMED {a.id} as {agent}")


def _require_owner(a, agent):
    t = _get(a.id)
    if not t:
        print(f"ERROR: no such task '{a.id}'"); sys.exit(1)
    if t.get("owner") != agent and not a.force:
        print(f"REFUSED: '{a.id}' owned by {t.get('owner') or '-'}, not {agent}. Use --force to override.")
        sys.exit(2)
    return t


def cmd_start(a, agent):
    t = _require_owner(a, agent); t["status"] = "in_progress"
    _save(t, agent); _beacon(agent, current=a.id, note=f"started {a.id}")
    print(f"IN_PROGRESS {a.id}")


def cmd_done(a, agent):
    t = _require_owner(a, agent); t["status"] = "done"
    if a.note:
        t["note"] = a.note
    _save(t, agent); _beacon(agent, current="", note=f"done {a.id}")
    print(f"DONE {a.id}")
    tasks = _all_tasks()
    unblocked = [x for x in tasks.values() if a.id in x.get("deps", []) and _ready(x, tasks)]
    if unblocked:
        print("  -> now READY (dependents unblocked):")
        for x in sorted(unblocked, key=lambda y: y["id"]):
            print(_fmt(x))


def cmd_block(a, agent):
    t = _require_owner(a, agent); t["status"] = "blocked"; t["note"] = a.reason or t.get("note", "")
    _save(t, agent); _beacon(agent, current=a.id, note=f"blocked {a.id}: {a.reason or ''}")
    print(f"BLOCKED {a.id}: {a.reason or ''}")


def cmd_release(a, agent):
    t = _require_owner(a, agent); t["owner"] = ""; t["status"] = "open"
    _save(t, agent); _beacon(agent, current="", note=f"released {a.id}")
    print(f"RELEASED {a.id} (back to open)")


def cmd_cancel(a, agent):
    t = _get(a.id)
    if not t:
        print(f"ERROR: no such task '{a.id}'"); sys.exit(1)
    t["status"] = "cancelled"; t["note"] = a.reason or t.get("note", "")
    _save(t, agent); print(f"CANCELLED {a.id}")


def cmd_note(a, agent):
    t = _get(a.id)
    if not t:
        print(f"ERROR: no such task '{a.id}'"); sys.exit(1)
    t["note"] = a.note; _save(t, agent); print(f"noted {a.id}: {a.note}")


def cmd_dep(a, agent):
    """Edit a task's dependency list. Clean escape from a cancelled/wrong dep without --force."""
    t = _get(a.id)
    if not t:
        print(f"ERROR: no such task '{a.id}'"); sys.exit(1)
    deps = list(t.get("deps", []))
    for d in (x.strip() for x in a.add.split(",")) if a.add else []:
        if d and d not in deps:
            deps.append(d)
    rm = {x.strip() for x in a.remove.split(",")} if a.remove else set()
    deps = [d for d in deps if d not in rm]
    t["deps"] = deps
    _save(t, agent)
    tasks = _all_tasks()
    state = "READY" if _ready(t, tasks) else ("STUCK" if _cancelled_deps(t, tasks) else t["status"])
    print(f"deps for {a.id}: {deps or '-'}  ({state})")


def cmd_beacon(a, agent):
    _beacon(agent, note=a.note or ""); print(f"beacon updated for {agent}")


def cmd_wake(a, agent):
    """Send an interrupt to the other agent's wake watcher (NON-retained so it doesn't replay on
    reconnect). A watcher subscribed to ha/agents/wake/<target> invokes a headless runner. Only works
    where the target has a wake watcher running (i.e. a box with the claude CLI)."""
    _pub(f"{BASE}/wake/{a.target}", {"from": agent, "reason": a.reason or "",
                                     "task": a.task or "", "ts": now()}, retain=False)
    print(f"woke {a.target}: {a.reason or '(no reason given)'}")


def cmd_whoami(a, agent):
    print(f"agent={agent}  broker={BROKER}:{PORT}  base={BASE}/")


def main():
    # Shared flags live on a parent parser attached to BOTH the top parser and every subparser, so
    # `--as/--broker/--force` work in either position. SUPPRESS => unset never clobbers across parsers.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--as", dest="agent", default=argparse.SUPPRESS,
                        help="agent id (ops|dev); or set $HA_AGENT_ID")
    common.add_argument("--broker", dest="broker", default=argparse.SUPPRESS,
                        help="override broker host (default VIP 192.168.0.200)")
    common.add_argument("--force", dest="force", action="store_true", default=argparse.SUPPRESS,
                        help="override ownership/readiness guards")

    p = argparse.ArgumentParser(parents=[common],
                                description="Agent-to-agent task coordination over the cluster bus.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name):
        return sub.add_parser(name, parents=[common])

    add("list").add_argument("--all", action="store_true", help="include done/cancelled")
    add("ready"); add("mine"); add("agents"); add("whoami")

    sp = add("add"); sp.add_argument("id"); sp.add_argument("--title", required=True)
    sp.add_argument("--deps", default=""); sp.add_argument("--note", default="")
    for name in ("claim", "start", "release"):
        add(name).add_argument("id")
    sp = add("done"); sp.add_argument("id"); sp.add_argument("--note", default="")
    sp = add("block"); sp.add_argument("id"); sp.add_argument("--reason", default="")
    sp = add("cancel"); sp.add_argument("id"); sp.add_argument("--reason", default="")
    sp = add("note"); sp.add_argument("id"); sp.add_argument("--note", required=True)
    sp = add("dep"); sp.add_argument("id"); sp.add_argument("--add", default=""); sp.add_argument("--remove", default="")
    sp = add("beacon"); sp.add_argument("--note", default="")
    sp = add("wake"); sp.add_argument("target"); sp.add_argument("--reason", default=""); sp.add_argument("--task", default="")

    a = p.parse_args()
    a.force = getattr(a, "force", False)
    global BROKER
    if getattr(a, "broker", None):
        BROKER = a.broker
    agent = getattr(a, "agent", None) or os.environ.get("HA_AGENT_ID")
    if a.cmd in ("list", "ready", "agents", "whoami") and not agent:
        agent = "anon"
    if not agent:
        print("ERROR: set --as <id> or $HA_AGENT_ID (ops|dev)"); sys.exit(1)
    if agent not in KNOWN_AGENTS and agent != "anon":
        print(f"WARN: agent '{agent}' not in {KNOWN_AGENTS}", file=sys.stderr)

    {"list": cmd_list, "ready": cmd_ready, "mine": cmd_mine, "agents": cmd_agents,
     "whoami": cmd_whoami, "add": cmd_add, "claim": cmd_claim, "start": cmd_start,
     "done": cmd_done, "block": cmd_block, "release": cmd_release, "cancel": cmd_cancel,
     "note": cmd_note, "dep": cmd_dep, "beacon": cmd_beacon, "wake": cmd_wake}[a.cmd](a, agent)


if __name__ == "__main__":
    main()
