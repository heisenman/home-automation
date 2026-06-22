"""Normalized communication-event vocabulary + pure classification/health logic (ADR-0012).

No I/O — the recorder persists/publishes these. The point is ONE vocabulary across all transports so
the controller (fail-safe), mesh router (reroute), and UI (health) react to the same events regardless
of whether the link is BLE, MQTT-to-node, or Midea LAN.
"""
from __future__ import annotations

from dataclasses import dataclass

# the canonical event vocabulary
REACHABLE = "reachable"        # a transport just confirmed it can talk to the device
UNREACHABLE = "unreachable"    # could not connect / not found / broker down
AUTH_EXPIRED = "auth_expired"  # credentials/token rejected or expired (e.g. Midea key rotation)
STALE = "stale"               # no fresh data within the freshness window
DEGRADED = "degraded"         # talks, but impaired (e.g. weak link, empty buffer, partial)
ACKED = "acked"               # a command was accepted + reconciled
NO_ACK = "no_ack"             # a command got no ack (device/broker silent)
REFUSED = "refused"           # device/policy rejected the command

KINDS = frozenset({REACHABLE, UNREACHABLE, AUTH_EXPIRED, STALE, DEGRADED, ACKED, NO_ACK, REFUSED})

# kinds that mean "currently not usable" vs "usable but impaired"
_OFFLINE = frozenset({UNREACHABLE, AUTH_EXPIRED, NO_ACK})
_IMPAIRED = frozenset({STALE, DEGRADED, REFUSED})
_HEALTHY = frozenset({REACHABLE, ACKED})


class CommsEventError(ValueError):
    pass


@dataclass(frozen=True)
class CommsEvent:
    ts: float
    device_id: str
    transport: str            # "ble-adv" | "ble-gatt" | "mqtt-node" | "midea-lan" | ...
    kind: str                 # one of KINDS
    detail: str = ""

    def __post_init__(self):
        if self.kind not in KINDS:
            raise CommsEventError(f"unknown comms-event kind {self.kind!r} (valid: {sorted(KINDS)})")


def event(ts: float, device_id: str, transport: str, kind: str, detail: str = "") -> CommsEvent:
    return CommsEvent(ts=ts, device_id=device_id, transport=transport, kind=kind, detail=detail)


# ── mappers: translate existing subsystem signals into the vocabulary (no rewrites) ──────────────
def from_pull_outcome(ok: bool, reason: str = "") -> str:
    """tools' history-pull outcome (pull_log) -> a comms-event kind."""
    if ok:
        return REACHABLE
    r = (reason or "").lower()
    if "connect" in r or "not found" in r or "unreachable" in r:
        return UNREACHABLE
    if "auth" in r or "token" in r or "key" in r or "expire" in r:
        return AUTH_EXPIRED
    if "empty" in r or "no_metadata" in r or "no metadata" in r or "partial" in r:
        return DEGRADED
    return UNREACHABLE


def from_issue_status(status: str) -> str:
    """issuer Result.status -> a comms-event kind."""
    return {
        "ok": ACKED,
        "no-ack": NO_ACK,
        "rejected": REFUSED,
        "mismatch": DEGRADED,
        "unknown-device": UNREACHABLE,
    }.get(status, NO_ACK)


# ── health derivation (pure) ──────────────────────────────────────────────────────
def health(events: list[CommsEvent], now: float, recent_s: float = 900.0) -> str:
    """Current health from the most recent event per (transport) within `recent_s`:
    'online' if the latest healthy event dominates, 'degraded' if impaired, 'offline' if not usable,
    'unknown' if no recent events. The single most-recent event wins (latest state of the link)."""
    recent = [e for e in events if now - e.ts <= recent_s]
    if not recent:
        return "unknown"
    latest = max(recent, key=lambda e: e.ts)
    if latest.kind in _OFFLINE:
        return "offline"
    if latest.kind in _IMPAIRED:
        return "degraded"
    return "online"


def is_actionable(events: list[CommsEvent], now: float, recent_s: float = 900.0) -> bool:
    """True if the device looks usable enough to send a command / trust its data right now.
    Used by the controller's fail-safe: offline/stale -> don't act on the rule, fall to default."""
    return health(events, now, recent_s) in ("online", "unknown")
