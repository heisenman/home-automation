"""ADR-0036 Layer 3 — claim an unclaimed edge node (TOFU) and register its node-born secret.

The model change ADR-0036 Layer 0 introduced: a node MINTS its own command secret on first boot
(``ha_config_ensure_node_secret``, after the radio is up so the HW RNG is seeded) instead of receiving
one baked into its firmware at build time. That decouples the image from the identity — one generic
image can flash any board — but it leaves the dictator not knowing the secret. This module closes that
gap: it asks the node for its secret **once** and records it.

The handover:

    dictator --> home/edge/<node>/enroll/req      (UNSIGNED — see below)
    node     --> home/edge/<node>/enroll/reply    {schema,node,mac,secret}   QoS1, NOT retained

**Why the request is unsigned.** The dictator cannot sign a request to a node whose secret it does not
yet hold — that is the bootstrap problem, and no amount of protocol design makes first contact
self-authenticating without pre-shared material. ADR-0036 accepts TOFU here (Hugh: "TOFU sounds fine")
because the bus is an air-gapped LAN with an anonymous broker. Two latches bound the exposure:

  * **Node-side one-shot** — the node honours exactly ONE unsigned enroll in the life of its NVS, then
    refuses (and stops subscribing to the topic entirely). Re-adoption needs an NVS wipe = physical
    presence, preserving the ADR-0010/0011 trust root.
  * **Dictator-side TOFU-lock** (here) — the first claim of a ``node_id`` BINDS it. A later claim of the
    same ``node_id`` presenting a different secret or MAC is REJECTED, never silently overwritten. This,
    not value collision, is the actual "fake node" guard: without it anyone could re-publish a reply and
    repoint an existing node's identity at hardware they control.

Failure is safe and loud in both directions: a hijacked claim burns the node's one shot, so the real
dictator's claim is refused and the operator sees a node stuck unadopted — rather than one silently
owned by someone else.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("ha.edge_enroll")

SECRET_RE = re.compile(r"^[0-9a-f]{64}$")            # HMAC-SHA256 hex, exactly as the firmware emits it
MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")
NODE_RE = re.compile(r"^[a-z0-9_]+$")

REQ_TOPIC = "home/edge/{node}/enroll/req"
REPLY_TOPIC = "home/edge/{node}/enroll/reply"


class ClaimError(Exception):
    """Claim could not be completed. The message is operator-facing."""


def _validate_reply(node_id: str, payload: dict) -> tuple[str, str]:
    """Return (mac, secret) from a node's enroll reply, or raise ClaimError."""
    if not isinstance(payload, dict):
        raise ClaimError("enroll reply was not a JSON object")
    if str(payload.get("node", "")) != node_id:
        # A reply on <node>'s topic claiming to be someone else — either a bug or someone fishing.
        raise ClaimError(f"enroll reply identifies as '{payload.get('node')}', expected '{node_id}'")
    secret = str(payload.get("secret", ""))
    if not SECRET_RE.match(secret):
        raise ClaimError("enroll reply carried no valid 64-hex-char secret")
    mac = str(payload.get("mac", ""))
    if mac and not MAC_RE.match(mac):
        raise ClaimError(f"enroll reply carried a malformed mac '{mac}'")
    return mac.upper(), secret


def register_claim(node_secrets_path: Path, master: str, node_id: str, mac: str, secret: str,
                   *, now: float | None = None) -> tuple[bool, str]:
    """Record a claimed node's secret in the encrypted LUT under the TOFU-lock.

    Returns (changed, message). ``changed`` is False for an idempotent re-claim (same node, same secret) —
    a retry after a dropped reply must not look like a failure. Raises ClaimError on a LOCK VIOLATION:
    the node_id already exists with a DIFFERENT secret or MAC.

    Write safety mirrors ``handle_enroll_node``: keep a .bak, save atomically, then verify the round-trip
    decrypt and roll back on mismatch — this LUT gates ALL node command auth, so a corrupt write must
    never be reported as success.
    """
    from server.control import secret_store as ss

    if not NODE_RE.match(node_id or ""):
        raise ClaimError("node_id must be a slug [a-z0-9_]")
    if not SECRET_RE.match(secret or ""):
        raise ClaimError("secret must be 64 hex chars")

    p = Path(node_secrets_path)
    lut = ss.load_lut(p, master)

    existing = lut.get(node_id)
    if existing is not None:
        # ── TOFU-lock ────────────────────────────────────────────────────────────────────────────
        # First claim of a node_id binds it. Re-presenting the SAME secret is a harmless retry; a
        # DIFFERENT one means either a node was re-flashed/NVS-wiped (legitimate, but needs a human to
        # say so) or someone is trying to hijack the identity. We cannot tell those apart from here, so
        # we refuse and surface it. Rotation stays an explicit, deliberate operation.
        old_secret = str(existing.get("cmd_secret", ""))
        old_mac = str(existing.get("mac", "") or "").upper()
        if old_secret == secret and (not mac or not old_mac or old_mac == mac):
            return False, f"node '{node_id}' already claimed with this secret (idempotent)"
        if old_secret != secret:
            raise ClaimError(
                f"TOFU-lock: node '{node_id}' is already enrolled with a DIFFERENT secret. Refusing to "
                f"overwrite. If this node was legitimately re-flashed or NVS-wiped, rotate it explicitly "
                f"(tools/enroll_node.py --rotate) — an automatic overwrite here would let anyone who can "
                f"publish an enroll reply seize an existing node's identity.")
        raise ClaimError(
            f"TOFU-lock: node '{node_id}' is enrolled against MAC {old_mac or '(none)'} but the claim "
            f"presented {mac}. Refusing — same node_id on different hardware.")

    lut[node_id] = {
        "mac": mac,
        "cmd_secret": secret,
        "mqtt_user": node_id,
        # No mqtt_pass: unlike handle_enroll_node we do NOT mint broker credentials here. The node already
        # exists and is talking to the anonymous broker; inventing a password it has never been told would
        # brick it the moment the broker-auth cutover happens.
        "created": int(now if now is not None else time.time()),
        "provisioning": "node-born",       # ADR-0036 L0 — distinguishes these from build-time enrolments
    }

    bak = p.with_suffix(p.suffix + ".bak")
    if p.exists():
        bak.write_bytes(p.read_bytes())
    ss.save_lut(p, master, lut)
    try:
        if ss.load_lut(p, master).get(node_id, {}).get("cmd_secret") != secret:
            raise ValueError("verify mismatch")
    except Exception:
        if bak.exists():
            p.write_bytes(bak.read_bytes())
        raise ClaimError("claim failed round-trip verification; rolled back — node NOT enrolled")
    return True, f"node '{node_id}' claimed and enrolled (node-born secret)"


def claim_node(node_id: str, *, broker: str, port: int = 1883, node_secrets_path: Path, master: str,
               timeout: float = 10.0) -> dict[str, Any]:
    """Ask <node> for its node-born secret and register it. Blocking; returns a result dict.

    Subscribes to the reply topic BEFORE publishing the request — the node replies immediately and a
    non-retained QoS-1 message is gone if nobody is listening.
    """
    import paho.mqtt.client as mqtt

    if not NODE_RE.match(node_id or ""):
        raise ClaimError("node_id must be a slug [a-z0-9_]")

    reply_topic = REPLY_TOPIC.format(node=node_id)
    req_topic = REQ_TOPIC.format(node=node_id)
    got: dict[str, Any] = {}
    done = threading.Event()

    def on_message(_c, _u, msg):
        if done.is_set():
            return
        try:
            got.update(json.loads(msg.payload.decode()))
        except Exception as e:            # noqa: BLE001 — a malformed reply must not hang the claim
            got["_parse_error"] = str(e)
        done.set()

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)     # paho v2
    except (AttributeError, TypeError):
        client = mqtt.Client()                                     # paho v1 (shared venv, see REUSE notes)
    client.on_message = on_message
    client.connect(broker, port, keepalive=30)
    client.loop_start()
    try:
        client.subscribe(reply_topic, qos=1)
        time.sleep(0.2)                       # let the SUBACK land before we prompt the node
        client.publish(req_topic, json.dumps({"schema": 1, "ts": int(time.time())}), qos=1, retain=False)
        if not done.wait(timeout):
            raise ClaimError(
                f"no enroll reply from '{node_id}' within {timeout:.0f}s. Either it is offline, it is "
                f"running pre-ADR-0036 firmware, or it has ALREADY been claimed (its one-shot enroll "
                f"window is closed — check `hello.enrolled`, and NVS-wipe the node to re-adopt).")
    finally:
        client.loop_stop()
        try:
            client.disconnect()
        except Exception:                     # noqa: BLE001 — teardown must not mask a real error
            pass

    if "_parse_error" in got:
        raise ClaimError(f"unparseable enroll reply: {got['_parse_error']}")
    mac, secret = _validate_reply(node_id, got)
    changed, message = register_claim(node_secrets_path, master, node_id, mac, secret)
    log.info("claim %s: %s", node_id, message)
    # Deliberately NOT returning the secret — it is written to the LUT and has no business in an API
    # response, a log line, or a PWA payload.
    return {"status": "claimed", "node_id": node_id, "mac": mac, "changed": changed, "detail": message}
