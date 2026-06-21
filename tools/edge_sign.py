"""Sign an edge-node command into the {p,s} envelope the firmware verifies (ADR-0010).

The node hashes the LITERAL `p` string, so we sign exactly the compact JSON we transmit — no
canonicalisation ambiguity. The per-device secret comes from $HA_CMD_SECRET (must equal the
HA_CMD_SECRET compiled into the node's secrets.h).

  inner = {"op": "gatt", "mac": "...", "steps": [...], "ts": <unix>}
  env   = {"p": "<compact json of inner>", "s": "<hmac-sha256 hex>"}
"""
import hashlib
import hmac
import json
import os
import time


def wrap(inner: dict, secret: str | None = None) -> dict:
    secret = secret or os.environ.get("HA_CMD_SECRET", "")
    if not secret:
        raise SystemExit("HA_CMD_SECRET not set — export the node's per-device command secret")
    inner = dict(inner)
    inner.setdefault("ts", int(time.time()))            # freshness; node rejects |dt|>60s
    p = json.dumps(inner, separators=(",", ":"))         # exact bytes we sign + transmit
    s = hmac.new(secret.encode(), p.encode(), hashlib.sha256).hexdigest()
    return {"p": p, "s": s}
