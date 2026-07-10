#!/usr/bin/env python3
"""Fence-over-bus (ADR-0033 failover-ssh-decouple, part 2).

Replaces the SSH fence (`ssh peer 'sudo systemctl stop <controller>'`) with a SIGNED command on the cluster
bus. The MASTER publishes a fence to `ha/cluster/fence`; each node's listener verifies the HMAC + freshness +
that it is the target, then stops its own controller unit. The bus is anonymous, so the HMAC (shared
`instance/.fence_key`) is the authorization — a random bus client can't fence.

  fence.py listen                              # daemon: act on valid fences targeting THIS host
  fence.py publish --target <host> --unit <u>  # MASTER publishes a signed fence
  fence.py selftest                            # verify sign/verify + reject tampered/stale (no side effects)

Env: FENCE_BROKER (127.0.0.1) FENCE_PORT (1883) FENCE_HOST (hostname) FENCE_KEY_FILE (instance/.fence_key)
     FENCE_MAX_AGE_S (30) FENCE_DRY_RUN (0 -> if 1, log instead of stopping)
"""
from __future__ import annotations
import hashlib, hmac, json, os, socket, subprocess, sys, time, uuid, logging

import paho.mqtt.client as mqtt

LOG = logging.getLogger("fence")
TOPIC = "ha/cluster/fence"


def _key() -> bytes:
    p = os.environ.get("FENCE_KEY_FILE", "instance/.fence_key")
    with open(p) as f:
        return f.read().strip().encode()


def _sig(key: bytes, target: str, unit: str, frm: str, ts: int, nonce: str) -> str:
    msg = f"{target}|{unit}|{frm}|{ts}|{nonce}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def make_fence(target: str, unit: str, frm: str) -> dict:
    ts, nonce = int(time.time()), uuid.uuid4().hex
    return {"target": target, "unit": unit, "from": frm, "ts": ts, "nonce": nonce,
            "sig": _sig(_key(), target, unit, frm, ts, nonce)}


def verify(msg: dict, max_age: int) -> tuple[bool, str]:
    try:
        want = _sig(_key(), msg["target"], msg["unit"], msg["from"], int(msg["ts"]), msg["nonce"])
    except (KeyError, TypeError, ValueError):
        return False, "malformed"
    if not hmac.compare_digest(want, str(msg.get("sig", ""))):
        return False, "bad-signature"
    age = abs(time.time() - int(msg["ts"]))
    if age > max_age:
        return False, f"stale({int(age)}s)"
    return True, "ok"


def _mk(cid):
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
    except (AttributeError, TypeError):
        return mqtt.Client(client_id=cid)


def _broker():
    return os.environ.get("FENCE_BROKER", "127.0.0.1"), int(os.environ.get("FENCE_PORT", "1883"))


def cmd_publish(target: str, unit: str) -> int:
    frm = os.environ.get("FENCE_HOST", socket.gethostname())
    payload = json.dumps(make_fence(target, unit, frm))
    host, port = _broker()
    c = _mk(f"fence-pub-{uuid.uuid4().hex[:6]}")
    c.connect(host, port, keepalive=10); c.loop_start()
    c.publish(TOPIC, payload, qos=1).wait_for_publish(timeout=5)
    c.loop_stop(); c.disconnect()
    LOG.info("published fence target=%s unit=%s via %s:%d", target, unit, host, port)
    return 0


def cmd_listen() -> int:
    me = os.environ.get("FENCE_HOST", socket.gethostname())
    max_age = int(os.environ.get("FENCE_MAX_AGE_S", "30"))
    dry = str(os.environ.get("FENCE_DRY_RUN", "0")).lower() in ("1", "true", "yes")
    seen: set[str] = set()

    def on_msg(_c, _u, m):
        try:
            msg = json.loads(m.payload)
        except Exception:
            return
        ok, why = verify(msg, max_age)
        if not ok:
            LOG.warning("REJECTED fence: %s (%s)", why, str(m.payload)[:120]); return
        if msg["nonce"] in seen:  # replay guard within process lifetime
            return
        seen.add(msg["nonce"])
        if msg["target"] != me:
            LOG.info("fence for %s (not me=%s) — ignoring", msg["target"], me); return
        unit = msg["unit"]
        LOG.warning("VALID fence from %s -> stop %s%s", msg["from"], unit, " [DRY-RUN]" if dry else "")
        if not dry:
            r = subprocess.run(["sudo", "systemctl", "stop", unit], capture_output=True, text=True)
            LOG.warning("fenced: systemctl stop %s rc=%d %s", unit, r.returncode, r.stderr.strip())

    host, port = _broker()
    c = _mk(f"fence-listen-{me}")
    c.on_connect = lambda cl, *_a: cl.subscribe(TOPIC, qos=1)
    c.on_message = on_msg
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    LOG.info("fence-listener up: me=%s broker=%s:%d max_age=%ds dry=%s", me, host, port, max_age, dry)
    c.connect(host, port, keepalive=30); c.loop_forever()
    return 0


def cmd_selftest() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    f = make_fence("ha-2", "ha-ag-controller", ".210")
    ok, why = verify(f, 30); assert ok, why
    LOG.info("valid fence verifies: %s", ok)
    bad = dict(f); bad["unit"] = "evil-unit"
    ok2, why2 = verify(bad, 30); assert not ok2, "tamper accepted!"
    LOG.info("tampered fence rejected: %s (%s)", not ok2, why2)
    old = make_fence("ha-2", "ha-ag-controller", ".210"); old["ts"] -= 3600
    old["sig"] = _sig(_key(), old["target"], old["unit"], old["from"], old["ts"], old["nonce"])
    ok3, why3 = verify(old, 30); assert not ok3, "stale accepted!"
    LOG.info("stale fence rejected: %s (%s)", not ok3, why3)
    LOG.info("SELFTEST PASS")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__); return 2
    cmd = sys.argv[1]
    if cmd == "publish":
        import argparse
        p = argparse.ArgumentParser(); p.add_argument("--target", required=True); p.add_argument("--unit", required=True)
        a = p.parse_args(sys.argv[2:]); logging.basicConfig(level=logging.INFO); return cmd_publish(a.target, a.unit)
    if cmd == "listen":
        return cmd_listen()
    if cmd == "selftest":
        return cmd_selftest()
    print(__doc__); return 2


if __name__ == "__main__":
    sys.exit(main())
