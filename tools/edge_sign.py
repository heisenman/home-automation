"""Sign an edge-node command into the {p,s} envelope the firmware verifies (ADR-0010).

The node hashes the LITERAL `p` string, so we sign exactly the compact JSON we transmit — no
canonicalisation ambiguity. The per-device secret comes from $HA_CMD_SECRET (must equal the
HA_CMD_SECRET compiled into the node's secrets.h).

  inner = {"op": "gatt", "mac": "...", "steps": [...], "ts": <unix>, "seq": <n>}
  env   = {"p": "<compact json of inner>", "s": "<hmac-sha256 hex>"}

Anti-replay: the firmware enforces a freshness WINDOW (300 s for gatt/history, 86400 s for ota) AND a
per-node MONOTONIC (ts, seq) guard — it acts on a command only if it is STRICTLY newer than the last it
acted on. `ts` is the primary key (so the scheme self-heals across a dictator rebuild: wall-clock only
advances); `seq` is the same-second tiebreaker so two commands stamped in the same second are still
ordered. We track (ts -> next seq) in-process: within one signer run, multiple commands in the same
second get increasing seq. No persistent server state is needed — across process restarts the clock has
moved on, so seq safely resets to 0.
"""
import hashlib
import hmac
import json
import os
import time

_last_ts = -1     # in-process: last ts we stamped
_last_seq = -1    # in-process: last seq we stamped at _last_ts


def wrap(inner: dict, secret: str | None = None) -> dict:
    global _last_ts, _last_seq
    secret = secret or os.environ.get("HA_CMD_SECRET", "")
    if not secret:
        raise SystemExit("HA_CMD_SECRET not set — export the node's per-device command secret")
    inner = dict(inner)
    ts = inner.setdefault("ts", int(time.time()))        # freshness; firmware rejects |dt|>window
    if "seq" not in inner:                               # monotonic tiebreaker within a wall-clock second
        seq = _last_seq + 1 if ts == _last_ts else 0
        inner["seq"] = seq
        _last_ts, _last_seq = ts, seq
    p = json.dumps(inner, separators=(",", ":"))         # exact bytes we sign + transmit
    s = hmac.new(secret.encode(), p.encode(), hashlib.sha256).hexdigest()
    return {"p": p, "s": s}
