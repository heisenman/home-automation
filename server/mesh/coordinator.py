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


def sign_envelope(secret: str, payload: dict) -> dict:
    """The firmware's `{p, s}` signed envelope (contract): p = the payload as a compact JSON STRING,
    s = HMAC-SHA256(node_secret, p). The firmware HMACs the literal p bytes it receives (cmd_sig_ok),
    so we send exactly the bytes we signed. sort_keys makes identical payloads sign identically."""
    p = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    s = hmac.new(secret.encode("utf-8"), p.encode("utf-8"), hashlib.sha256).hexdigest()
    return {"p": p, "s": s}


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
                 dwell_s: float = DEFAULT_DWELL_S, log_fn=print) -> dict:
    """One reconcile/publish pass. With client=None it's a DRY-RUN (prints decisions, no MQTT). Returns a
    summary {published:[nodes], pending:[nodes], skipped_no_secret:[nodes]}. A node that fell out of the
    graph but still has state is reconciled toward an empty allowlist (tell it to stop relaying)."""
    now = now if now is not None else time.time()
    per_node, local_devs = compute_allowlists(mesh_store.load_links(conn), dev_to_mac, now=now)
    state = mesh_store.load_relay_state(conn)
    nodes = sorted(set(per_node) | set(state))
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
            if client is not None:
                client.publish(topic, json.dumps(env), qos=1, retain=True)
                log_fn(f"PUBLISH {topic}  epoch={new['epoch']} relay={len(desired)} {desired}")
            else:
                log_fn(f"# DRY-RUN would PUBLISH {topic}  epoch={new['epoch']} relay={len(desired)} {desired}")
                log_fn(f"#   envelope={json.dumps(env)}")
            mesh_store.save_relay_state(conn, nid, new["epoch"], new["relay_macs"], None, None)
            out["published"].append(nid)
        elif action in ("pending", "clear"):
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
    conn = sqlite3.connect(str(mp))
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
        if not lut:
            print("!! --publish refused: no node-secrets LUT / master available (can't sign). Staying dry-run.")
            a.publish = False
        else:
            import paho.mqtt.client as mqtt
            client = mqtt.Client()
            client.connect(a.broker, a.port, keepalive=30)
            client.loop_start()
            print(f"# relay-coordinator LIVE PUBLISH -> {a.broker}:{a.port} (dwell {a.dwell:.0f}s)")

    def one_pass():
        out = publish_pass(conn, dev_to_mac, lut, client=client, dwell_s=a.dwell)
        mode = "PUBLISH" if client else "dry-run"
        print(f"# [{mode}] local={len(out['local_devs'])} | published={out['published'] or '-'} | "
              f"pending(dwell)={out['pending'] or '-'} | no-secret={out['skipped_no_secret'] or '-'}")
        return out

    try:
        one_pass()
        while a.loop > 0:
            time.sleep(a.loop)
            one_pass()
    finally:
        if client is not None:
            client.loop_stop()
            client.disconnect()


if __name__ == "__main__":
    main()
