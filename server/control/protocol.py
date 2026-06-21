"""Command/ack wire protocol + authentication (plan §9, §13.3).

The "security handshake" for telling a node to act. Every command the dictator issues is:
  - identified (`id`), addressed (`device`/`node`), and time-stamped (`ts`);
  - carries a server-minted **freshness nonce** (replay defense for sensitive actions);
  - **signed with the node's per-device secret** via HMAC-SHA256 over a canonical encoding.

A node (or the simulated device) verifies the signature with its own secret, checks the timestamp
is within a freshness window, and — for sensitive actions — that the nonce has not been seen before.
A command that fails any check is refused. This means: even on today's anonymous broker, a forged or
replayed command is rejected, because the attacker doesn't hold the per-device secret.

HMAC (not full PKI) is deliberate: offline-first, per-device credentials (plan §13.3), and cheap to
verify on an MCU (mbedtls HMAC-SHA256 is already linked on the C6 for OTA TLS).

Pure stdlib, no I/O — unit-testable.
"""
from __future__ import annotations

import hmac
import json
import secrets as _secrets
import time
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any


PROTOCOL_VERSION = 1
DEFAULT_MAX_AGE_S = 30        # a command older than this is stale (clock-sync'd nodes, plan §10)


def make_nonce(nbytes: int = 16) -> str:
    return _secrets.token_hex(nbytes)


def _canonical(payload: dict[str, Any]) -> bytes:
    """Deterministic encoding for signing: the signed fields only, sorted keys, compact, UTF-8.
    The `sig` field itself is never part of the signed bytes."""
    signed = {k: v for k, v in payload.items() if k != "sig"}
    return json.dumps(signed, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign(secret: str, payload: dict[str, Any]) -> str:
    return hmac.new(secret.encode("utf-8"), _canonical(payload), sha256).hexdigest()


def build_command(*, device: str, node: str, trait: str, action: str,
                  args: dict[str, Any], secret: str, sensitive: bool,
                  ts: float | None = None, cmd_id: str | None = None) -> dict[str, Any]:
    """Assemble and sign a command. `ts` is injectable for tests (Date.now is otherwise wall-clock)."""
    cmd = {
        "v": PROTOCOL_VERSION,
        "id": cmd_id or make_nonce(8),
        "device": device,
        "node": node,
        "trait": trait,
        "action": action,
        "args": args,
        "nonce": make_nonce(),
        "sensitive": bool(sensitive),
        "ts": float(ts if ts is not None else time.time()),
    }
    cmd["sig"] = sign(secret, cmd)
    return cmd


@dataclass
class Verifier:
    """Node-side command verifier. Holds the per-device secret and a bounded nonce cache so a
    sensitive command's nonce can't be replayed. `now` is injectable for tests."""
    secret: str
    max_age_s: float = DEFAULT_MAX_AGE_S
    _seen: set[str] = field(default_factory=set)
    _seen_order: list[str] = field(default_factory=list)
    _seen_cap: int = 4096

    def verify(self, cmd: dict[str, Any], now: float | None = None) -> tuple[bool, str]:
        """Return (ok, reason). reason == 'ok' on success."""
        if cmd.get("v") != PROTOCOL_VERSION:
            return False, "bad-version"
        sig = cmd.get("sig")
        if not isinstance(sig, str):
            return False, "missing-sig"
        expected = sign(self.secret, cmd)
        if not hmac.compare_digest(sig, expected):   # constant-time
            return False, "bad-sig"
        now = time.time() if now is None else now
        ts = cmd.get("ts")
        if not isinstance(ts, (int, float)):
            return False, "bad-ts"
        if abs(now - ts) > self.max_age_s:
            return False, "stale"
        # Sensitive actions must not replay a nonce. (Non-sensitive: sig+freshness suffice.)
        if cmd.get("sensitive"):
            nonce = cmd.get("nonce")
            if not isinstance(nonce, str):
                return False, "missing-nonce"
            if nonce in self._seen:
                return False, "replay"
            self._remember(nonce)
        return True, "ok"

    def _remember(self, nonce: str) -> None:
        self._seen.add(nonce)
        self._seen_order.append(nonce)
        if len(self._seen_order) > self._seen_cap:
            old = self._seen_order.pop(0)
            self._seen.discard(old)


# ── OTA directive (plan §13.3, ADR-0005): authenticate the directive AND bind the image ──────────
def image_sha256(data: bytes) -> str:
    return sha256(data).hexdigest()


def build_ota_directive(*, node: str, url: str, sha256_hex: str, version: int,
                        secret: str, ts: float | None = None) -> dict[str, Any]:
    """A SIGNED firmware directive. The signature covers url+sha256+version, so a node holding the
    per-device secret can trust the directive's origin AND verify the downloaded image's integrity
    (compare its sha256 to this signed value) before flashing — rollback stops bricking, this stops
    malice. OTA is sensitive → nonce-gated against replay. Plain-HTTP transport is acceptable because
    integrity rides the signed hash, not TLS (air-gap friendly)."""
    d = {
        "v": PROTOCOL_VERSION,
        "op": "ota",
        "id": make_nonce(8),
        "node": node,
        "url": url,
        "sha256": sha256_hex,
        "version": int(version),
        "nonce": make_nonce(),
        "sensitive": True,
        "ts": float(ts if ts is not None else time.time()),
    }
    d["sig"] = sign(secret, d)
    return d


def check_ota_image(directive: dict[str, Any], downloaded_sha256: str,
                    current_version: int) -> tuple[bool, str]:
    """Node-side gate AFTER the directive signature has been verified (via Verifier) and the image
    downloaded. Confirms the image matches the signed hash and is not a downgrade (soft anti-rollback
    — NOT eFuse-based, preserving USB recovery per ADR-0005)."""
    if not hmac.compare_digest(directive.get("sha256", ""), downloaded_sha256):
        return False, "image-hash-mismatch"
    if int(directive.get("version", -1)) <= int(current_version):
        return False, "downgrade-refused"
    return True, "ok"


def build_ack(*, cmd_id: str, status: str, reported_state: dict[str, Any] | None = None,
              source: str = "commanded", reason: str = "", ts: float | None = None) -> dict[str, Any]:
    """Device→server ack. Closed loop: intended (command) vs reported_state (actual) vs source."""
    return {
        "v": PROTOCOL_VERSION,
        "id": cmd_id,
        "status": status,                 # "ok" | "rejected" | "error"
        "reason": reason,
        "reported_state": reported_state or {},
        "source": source,                 # "commanded" | "autonomous" | "manual"
        "ts": float(ts if ts is not None else time.time()),
    }
