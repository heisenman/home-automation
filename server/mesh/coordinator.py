"""ha-relay-coordinator — ADR-0015 Phase B / decision #4.

Computes each edge node's relay allowlist from the mesh reach graph (server/mesh/store mesh.db) and the
registry, and publishes a signed, retained `relay_assign` directive on `home/edge/<node>/relay` so nodes
stop relaying meters they're NOT the preferred source for (saves edge radio/energy). Tier-1 (the mapper)
already drops redundant data server-side; this stops the redundant TRANSMISSION at the node.

Contract: docs/decisions/phase-b-relay-directive-contract.md. DEFAULT DRY-RUN — prints the directives it
would send; live publish (--publish) needs the firmware consumer (dev) and per-node signing (master LUT).
The allowlist math is pure + unit-tested (compute_allowlists).
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
from server.mesh import store as mesh_store          # noqa: E402
from server.mesh.topology import SERVER, Link, best_relay, build_graph  # noqa: E402

log = logging.getLogger("ha.relay_coordinator")


def compute_allowlists(links, dev_to_mac: dict, now: float | None = None):
    """From observed links, return (per_node, local_devs):
      per_node  = {node_id: {"device_ids": [...], "relay_macs": [...]}}  — edge nodes that are the
                  preferred source for >=1 device (their relay allowlist).
      local_devs = device_ids the dictator's own radio ('local') is preferred for (no edge directive).
    A node's allowlist is exactly the devices best_relay picks IT for; everything else it should drop."""
    # mesh.db records only receiver->endpoint reach, NOT the SERVER->node IP backhaul. Inject the implicit
    # backhaul (every edge node is reachable over MQTT/IP) so best_relay can actually route through a node;
    # without it edge nodes are disconnected from SERVER and local always wins by default.
    edge_nodes = {l.src for l in links if l.src[0] == "node"}
    links = list(links) + [Link(SERVER, n, "ip") for n in edge_nodes]
    g = build_graph(links)
    endpoints = sorted({n for n in g.nodes if n[0] == "endpoint"})
    per_node: dict[str, dict] = {}
    local_devs: list[str] = []
    for ep in endpoints:
        node, hops, cost = best_relay(g, ep, src=SERVER)
        if node is None:
            continue
        did = ep[1]
        if node == SERVER:
            local_devs.append(did)
            continue
        nid = node[1]
        slot = per_node.setdefault(nid, {"device_ids": [], "relay_macs": []})
        slot["device_ids"].append(did)
        mac = dev_to_mac.get(did)
        if mac:
            slot["relay_macs"].append(mac)
    return per_node, local_devs


def build_directive(relay_macs: list[str], epoch: int, ttl_s: int = 3600) -> dict:
    return {"schema": 1, "type": "relay_assign", "epoch": epoch,
            "relay_macs": sorted(relay_macs), "cmd_relay": [], "ttl_s": ttl_s}


DEFAULT_DWELL_S = 900.0          # decision #5: a node's set must hold this long before we re-publish
RELAY_TOPIC = "home/edge/{node}/relay"
REACH_REQ_TOPIC = "home/edge/{node}/reach/req"   # ADR-0023: signed census trigger (server-push cadence)
EVENT_TOPIC = "home/edge/+/event"                # ADR-0023 inverse: node-pushed state-change events


def sign_envelope(secret: str, payload: dict) -> dict:
    """The firmware's `{p, s}` signed envelope (contract): p = the payload as a compact JSON STRING,
    s = HMAC-SHA256(node_secret, p). The firmware HMACs the literal p bytes it receives (cmd_sig_ok),
    so we send exactly the bytes we signed. sort_keys makes identical payloads sign identically."""
    p = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    s = hmac.new(secret.encode("utf-8"), p.encode("utf-8"), hashlib.sha256).hexdigest()
    return {"p": p, "s": s}


def trigger_reach(client, lut: dict) -> int:
    """ADR-0023 server-push census: publish a signed trigger to every enrolled node so the whole mesh
    reports its reach neighborhood at a coordinated instant. Fired once per reconcile pass; responses
    land on home/edge/<node>/reach (ingested by the mapper) and feed the NEXT pass's best_relay — so
    relocated/relay-none nodes stay visible and rebalancing sees un-relayed reach. Sig-only + idempotent
    (the firmware handler does no anti-replay), so it can't collide with real actuation commands. A node
    keeps a long autonomous fallback, so a silent coordinator never makes a node invisible. Returns count."""
    if client is None:
        return 0
    n = 0
    for nid, info in sorted(lut.items()):
        secret = (info or {}).get("cmd_secret")
        if not secret:
            continue
        env = sign_envelope(secret, {"op": "reach"})
        client.publish(REACH_REQ_TOPIC.format(node=nid), json.dumps(env), qos=1, retain=False)
        n += 1
    return n


# ── ADR-0023 inverse: event-driven reconcile ─────────────────────────────────────────────────────────
# The census (trigger_reach) is the server-PUSH cadence: we ask, nodes report. This is its INVERSE — a
# node PUSHES a state-change event (retained, on home/edge/<node>/event) the instant its relay posture
# flips, so the coordinator can reconcile *now* instead of waiting for the next --loop pass. Asymmetric by
# design: a relay-DROP is urgent (a source just vanished — recompute promptly so a neighbour takes over),
# a relay-RETURN is lazy (dwelled, like any steady rebalance — no need to churn epochs for a node that
# might flap). Events are unsigned/LAN-trusted because they only ever PROMPT a recompute from already-
# verified reach in mesh.db; they can't inject a directive. Pure decision core here, thread shell in main().

def parse_event(topic: str, payload) -> tuple:
    """home/edge/<node>/event + JSON {node,kind,state:{on_wall,relaying,censusing},ts} → (node, state).
    Returns (None, None) on any malformed topic/payload/state so a bad publish can never crash the loop."""
    parts = topic.split("/")
    if len(parts) != 4 or parts[0] != "home" or parts[1] != "edge" or parts[3] != "event":
        return None, None
    node = parts[2]
    try:
        msg = json.loads(payload)
    except (ValueError, TypeError):
        return None, None
    state = msg.get("state") if isinstance(msg, dict) else None
    if not isinstance(state, dict):
        return None, None
    return node, state


class EventReconciler:
    """Maps a node's observed state to a reconcile *intent* on a `relaying` transition (nothing else).
    Stateful only in the last-seen state per node; the very first event for a node (typically the retained
    snapshot delivered on subscribe) is NOT a transition, so it records and returns None — this is what
    stops a reconcile storm at startup. Pure + deterministic → unit-tested without threads or MQTT."""

    def __init__(self, dwell_return: float = DEFAULT_DWELL_S):
        self._prev: dict[str, dict] = {}
        self._dwell_return = dwell_return

    def observe(self, node: str, state: dict) -> dict | None:
        prev = self._prev.get(node)
        self._prev[node] = state
        if prev is None:
            return None                                    # first sighting / retained snapshot: no transition
        was, now = bool(prev.get("relaying")), bool(state.get("relaying"))
        if was and not now:                                # relay dropped → urgent, census-then-reconcile now
            return {"node": node, "dwell_s": 0.0, "census": True, "reason": "relay-drop"}
        if not was and now:                                # relay returned → lazy, dwelled like any rebalance
            return {"node": node, "dwell_s": self._dwell_return, "census": False, "reason": "relay-return"}
        return None                                        # relaying unchanged → nothing to do


class EventDispatcher:
    """Coalesce + per-node rate-limit around EventReconciler. Time is injected (`now`) so the whole
    scheduler is unit-testable with a fake clock. A drop's census fires IMMEDIATELY (once per pending
    window) so neighbour reach freshens in mesh.db *during* the coalesce delay — by the time the reconcile
    actually runs it sees the post-drop graph. The reconcile itself is deferred `coalesce_s` (a burst of
    events for one node collapses to a single recompute) and throttled to one per `rate_limit_s`."""

    def __init__(self, reconciler: EventReconciler, coalesce_s: float = 5.0, rate_limit_s: float = 30.0):
        self.r = reconciler
        self.coalesce_s = coalesce_s
        self.rate_limit_s = rate_limit_s
        self._pending: dict[str, dict] = {}                # node -> {intent, ready_at, census_fired}
        self._last_fire: dict[str, float] = {}

    def observe(self, node: str, state: dict, now: float) -> dict | None:
        """Feed one event. Returns {'census_now': bool} when a census should be published right now
        (drop only, once per window), else None. The reconcile it schedules surfaces later via due()."""
        intent = self.r.observe(node, state)
        if intent is None:
            return None
        slot = self._pending.get(node, {})
        slot["intent"] = intent                            # newest intent for a node wins
        slot["ready_at"] = now + self.coalesce_s           # a fresh event resets the coalesce window
        self._pending[node] = slot
        fire = bool(intent["census"]) and not slot.get("census_fired")
        if fire:
            slot["census_fired"] = True
        return {"census_now": fire} if fire else None

    def due(self, now: float) -> list[dict]:
        """Intents whose coalesce window elapsed and that pass the per-node rate-limit. Rate-limited nodes
        stay pending (their window is pushed out), so an event is deferred, never dropped."""
        out = []
        for node in list(self._pending):
            slot = self._pending[node]
            if now < slot["ready_at"]:
                continue
            last = self._last_fire.get(node)
            if last is not None and now - last < self.rate_limit_s:
                slot["ready_at"] = last + self.rate_limit_s
                continue
            out.append(slot["intent"])
            self._last_fire[node] = now
            del self._pending[node]
        return out


def _state(epoch, published, pending, since) -> dict:
    return {"epoch": epoch, "relay_macs": sorted(published),
            "pending_macs": (sorted(pending) if pending is not None else None), "pending_since": since}


def reconcile(state: dict | None, desired, now: float, dwell_s: float = DEFAULT_DWELL_S):
    """Decide what to do for ONE node given its persisted relay_state and the freshly-computed desired
    allowlist. Debounced (decision #5): a changed set must hold for `dwell_s` before we re-publish, so
    borderline meters that flap run-to-run don't churn the firmware allowlist / burn epochs.

    Returns (action, new_state):
      'publish' -> commit: epoch bumped, caller signs+publishes desired, clears pending.
      'pending' -> a (new) candidate started/refreshed its dwell; persist it, do NOT publish.
      'clear'   -> already-correct but a stale pending lingers; persist the cleared state.
      'noop'    -> nothing to write (steady, or still dwelling).
    epoch 0 == nothing published yet (firmware default = relay-all)."""
    desired = sorted(desired)
    epoch = state["epoch"] if state else 0
    published = sorted(state["relay_macs"]) if state else []
    pending = state["pending_macs"] if state else None
    since = state["pending_since"] if state else None

    if epoch > 0 and published == desired:                 # already serving the right set
        if pending is not None:
            return "clear", _state(epoch, published, None, None)
        return "noop", None
    if pending is None or sorted(pending) != desired:       # new/changed candidate -> start the dwell now
        if dwell_s <= 0:                                    # no debounce -> commit immediately
            return "publish", _state(epoch + 1, desired, None, None)
        return "pending", _state(epoch, published, desired, now)
    if now - (since if since is not None else now) >= dwell_s:   # unchanged candidate, dwell elapsed
        return "publish", _state(epoch + 1, desired, None, None)
    return "noop", None                                     # unchanged candidate, still dwelling


def publish_pass(conn, dev_to_mac: dict, lut: dict, *, client=None, now: float | None = None,
                 dwell_s: float = DEFAULT_DWELL_S, only: set | None = None, log_fn=print) -> dict:
    """One reconcile/publish pass. client=None ⇒ DRY-RUN: prints decisions, **no MQTT and no DB writes**
    (a real publish is what advances epoch/dwell state — so a dry-run can never make a later publish wrongly
    skip). `only` limits the pass to a node subset (canary). A node that fell out of the graph but still has
    state is reconciled toward an empty allowlist (tell it to stop relaying)."""
    now = now if now is not None else time.time()
    persist = client is not None
    per_node, local_devs = compute_allowlists(mesh_store.load_links(conn), dev_to_mac, now=now)
    state = mesh_store.load_relay_state(conn)
    nodes = sorted(set(per_node) | set(state))
    if only is not None:
        nodes = [n for n in nodes if n in only]
    out = {"published": [], "pending": [], "skipped_no_secret": [], "local_devs": sorted(local_devs)}
    for nid in nodes:
        desired = sorted(per_node.get(nid, {}).get("relay_macs", []))
        action, new = reconcile(state.get(nid), desired, now, dwell_s)
        if action == "publish":
            secret = (lut.get(nid) or {}).get("cmd_secret")
            directive = build_directive(desired, new["epoch"])
            if not secret:
                out["skipped_no_secret"].append(nid)
                log_fn(f"# SKIP {nid}: no enrolled cmd_secret (un-provisioned) — would publish epoch "
                       f"{new['epoch']} relay {len(desired)}")
                continue
            env = sign_envelope(secret, directive)
            topic = RELAY_TOPIC.format(node=nid)
            if persist:
                client.publish(topic, json.dumps(env), qos=1, retain=True)
                mesh_store.save_relay_state(conn, nid, new["epoch"], new["relay_macs"], None, None)
                log_fn(f"PUBLISH {topic}  epoch={new['epoch']} relay={len(desired)} {desired}")
            else:
                log_fn(f"# DRY-RUN would PUBLISH {topic}  epoch={new['epoch']} relay={len(desired)} {desired}")
                log_fn(f"#   envelope={json.dumps(env)}")
            out["published"].append(nid)                   # in dry-run this means 'would publish'
        elif action in ("pending", "clear"):
            if persist:
                mesh_store.save_relay_state(conn, nid, new["epoch"], new["relay_macs"],
                                            new["pending_macs"], new["pending_since"])
            if action == "pending":
                out["pending"].append(nid)
    return out


def _load_registry_dev_to_mac(path: Path) -> dict:
    import yaml
    raw = yaml.safe_load(path.read_text()) if path.exists() else {}
    out = {}
    for mac, info in (raw.get("devices", {}) or {}).items():
        did = info.get("device_id")
        if did:
            out[did] = mac.upper()
    return out


def _load_lut(a) -> dict:
    """Per-node enrolled secrets (master-decrypted; dictator-only). Empty if no master/LUT — then
    publish can't sign and falls back to a preview that flags un-provisioned nodes."""
    try:
        from server.control.secret_store import available_master, load_lut
        master = available_master()
        if not master:
            return {}
        lut_path = a.node_secrets if a.node_secrets.is_absolute() else REPO_ROOT / a.node_secrets
        return load_lut(lut_path, master)
    except Exception:
        log.warning("could not load node-secrets LUT — signing disabled", exc_info=True)
        return {}


def _connect(a):
    mp = a.mesh_db if a.mesh_db.is_absolute() else REPO_ROOT / a.mesh_db
    # check_same_thread=False: the event thread and the main --loop share this conn, serialized by a lock.
    conn = sqlite3.connect(str(mp), check_same_thread=False)
    mesh_store.ensure_schema(conn)
    return conn


def main() -> None:
    p = argparse.ArgumentParser(description="ADR-0015 Phase B relay-assignment coordinator")
    p.add_argument("--mesh-db", default="instance/db/mesh.db", type=Path)
    p.add_argument("--registry", default="instance/devices.yaml", type=Path)
    p.add_argument("--node-secrets", default="instance/node_secrets.enc", type=Path)
    p.add_argument("--publish", action="store_true",
                   help="SIGN + PUBLISH retained directives to home/edge/<node>/relay (LIVE). Default = dry-run.")
    p.add_argument("--loop", type=float, default=0,
                   help="re-run every N seconds (decision #5 periodic backstop). 0 = single pass.")
    p.add_argument("--dwell", type=float, default=DEFAULT_DWELL_S,
                   help="seconds a changed allowlist must hold before re-publishing (debounce)")
    p.add_argument("--only", default="", help="comma-separated node ids to limit this pass to (canary)")
    p.add_argument("--events", dest="events", action="store_true", default=True,
                   help="subscribe home/edge/+/event for event-driven reconcile (default on with --publish --loop)")
    p.add_argument("--no-events", dest="events", action="store_false",
                   help="disable event-driven reconcile; rely only on the periodic --loop backstop")
    p.add_argument("--event-coalesce", type=float, default=5.0, help="event-driven reconcile coalesce window (s)")
    p.add_argument("--event-rate-limit", type=float, default=30.0, help="min seconds between event-driven reconciles per node")
    p.add_argument("--broker", default=os.environ.get("HA_BROKER", "localhost"))
    p.add_argument("--port", type=int, default=int(os.environ.get("HA_BROKER_PORT", "1883")))
    p.add_argument("--log-level", default="INFO")
    a = p.parse_args()
    logging.basicConfig(level=getattr(logging, a.log_level), format="%(message)s", stream=sys.stdout)

    conn = _connect(a)
    dev_to_mac = _load_registry_dev_to_mac(a.registry if a.registry.is_absolute() else REPO_ROOT / a.registry)
    lut = _load_lut(a)

    client = None
    if a.publish:
        vip = os.environ.get("HA_VIP", "")
        if vip:                                            # only the dictator publishes (no split-brain)
            try:
                from server.cluster.state import vip_held
                if not vip_held(vip):
                    print(f"!! --publish refused: this node does not hold VIP {vip} "
                          "(standby never publishes; notify.sh restarts on promote). Dry-run.")
                    a.publish = False
            except Exception:
                log.warning("VIP-held check failed; refusing to publish for safety", exc_info=True)
                a.publish = False
        if a.publish and not lut:
            print("!! --publish refused: no node-secrets LUT / master available (can't sign). Staying dry-run.")
            a.publish = False
        if a.publish:
            import paho.mqtt.client as mqtt
            try:                                           # paho >=2 wants an explicit callback API version
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            except (AttributeError, TypeError):
                client = mqtt.Client()
            client.connect(a.broker, a.port, keepalive=30)
            client.loop_start()
            print(f"# relay-coordinator LIVE PUBLISH -> {a.broker}:{a.port} (dwell {a.dwell:.0f}s)")

    only = {n.strip() for n in a.only.split(",") if n.strip()} or None

    db_lock = threading.Lock()          # serializes conn/relay_state writes across the loop + event threads

    def one_pass():
        with db_lock:
            out = publish_pass(conn, dev_to_mac, lut, client=client, dwell_s=a.dwell, only=only)
        # ADR-0023: after reconciling on the reach we already have, ask every node to re-census. Their
        # responses land during the --loop sleep and sharpen the NEXT pass (organic rebalance, no fossils).
        triggered = trigger_reach(client, lut) if client else 0
        mode = "PUBLISH" if client else "dry-run"
        print(f"# [{mode}] local={len(out['local_devs'])} | published={out['published'] or '-'} | "
              f"pending(dwell)={out['pending'] or '-'} | no-secret={out['skipped_no_secret'] or '-'} | "
              f"reach-triggered={triggered}")
        return out

    # ── ADR-0023 inverse: event-driven reconcile (live client + looping only) ────────────────────────
    stop = threading.Event()
    ev_thread = None
    events_on = bool(client) and a.loop > 0 and a.events
    if events_on:
        dispatcher = EventDispatcher(EventReconciler(dwell_return=a.dwell),
                                     coalesce_s=a.event_coalesce, rate_limit_s=a.event_rate_limit)

        def on_event_message(_cl, _ud, msg):
            node, state = parse_event(msg.topic, getattr(msg, "payload", b""))
            if node is None:
                return
            res = dispatcher.observe(node, state, time.time())
            if res and res.get("census_now"):              # drop → freshen neighbour reach during coalesce
                trigger_reach(client, lut)
                print(f"# event: {node} relay-drop → census fired, reconcile in {a.event_coalesce:.0f}s")

        client.on_message = on_event_message
        client.subscribe(EVENT_TOPIC, qos=1)

        def event_loop():
            while not stop.is_set():
                for it in dispatcher.due(time.time()):
                    with db_lock:
                        publish_pass(conn, dev_to_mac, lut, client=client,
                                     dwell_s=it["dwell_s"], only={it["node"]})
                    print(f"# event-reconcile: {it['node']} ({it['reason']}) dwell={it['dwell_s']:.0f}s")
                stop.wait(1.0)

        ev_thread = threading.Thread(target=event_loop, name="event-reconcile", daemon=True)
        ev_thread.start()
        print(f"# event-driven reconcile ARMED: {EVENT_TOPIC} "
              f"(coalesce {a.event_coalesce:.0f}s, rate-limit {a.event_rate_limit:.0f}s/node)")

    try:
        one_pass()
        while a.loop > 0:
            time.sleep(a.loop)
            one_pass()
    finally:
        stop.set()
        if ev_thread is not None:
            ev_thread.join(timeout=2.0)
        if client is not None:
            client.loop_stop()
            client.disconnect()


if __name__ == "__main__":
    main()
