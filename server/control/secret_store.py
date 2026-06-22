"""Encrypted per-device secret store + software confirm token (plan §13, ADR-0010/0011).

Trust model (Hugh, 2026-06-21): the server is the root of trust — if it's compromised the system is
already lost — so all per-node secrets live server-side. Encryption-at-rest (this module) therefore
protects **backups / disk theft**, not a live-compromised server: a leaked LUT file is useless without
the master passphrase.

- **LUT**: `{node_id: {mac, cmd_secret, mqtt_user, mqtt_pass, created}}`, Fernet-encrypted with a key
  scrypt-derived from the master passphrase (per-file random salt). `cmd_secret` is the per-device
  HMAC key baked into that node's firmware (ADR-0010) and used by the PEP to sign its commands.
- **Confirm token** = `SHA256("ha-confirm:" + master)`. The software second factor for sensitive
  actuator actions (no physical button needed). SHA is one-way, so the *hot* confirm value (typed at
  the API) never reveals the *cold* master that protects the LUT — Hugh's "two separate entities".

Master passphrase comes from $HA_MASTER_PASSPHRASE or a gitignored 0600 file (never committed, ideally
not in backups so the encrypted LUT is meaningless on its own).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

_CONFIRM_LABEL = "ha-confirm:"
_API_LABEL = "ha-api:"
_DEFAULT_MASTER_FILE = Path.home() / "home_automation/instance/.master_pass"


# ── master passphrase ──────────────────────────────────────────────────────────
def load_master(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("HA_MASTER_PASSPHRASE")
    if env:
        return env
    # explicit file path (HOME-independent — e.g. ha-api overrides $HOME for DuckDB scratch, which would
    # otherwise misplace the default below):
    fpath = os.environ.get("HA_MASTER_PASS_FILE")
    if fpath and Path(fpath).exists():
        return Path(fpath).read_text().strip()
    if _DEFAULT_MASTER_FILE.exists():
        return _DEFAULT_MASTER_FILE.read_text().strip()
    raise SystemExit("master passphrase not set — export HA_MASTER_PASSPHRASE, set HA_MASTER_PASS_FILE, "
                     f"or create {_DEFAULT_MASTER_FILE} (0600)")


def available_master(explicit: str | None = None) -> str | None:
    """Non-raising variant for optional features (e.g. mounting the control plane): returns the master
    if configured (arg / $HA_MASTER_PASSPHRASE / the 0600 file), else None — never aborts the process."""
    try:
        return load_master(explicit)
    except SystemExit:
        return None


# ── LUT encryption (at rest) ─────────────────────────────────────────────────────
def _key(passphrase: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=2 ** 14, r=8, p=1)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


def load_lut(path: str | Path, passphrase: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    env = json.loads(p.read_text())
    try:
        pt = Fernet(_key(passphrase, bytes.fromhex(env["salt"]))).decrypt(env["data"].encode())
    except (InvalidToken, KeyError, ValueError):
        raise ValueError("cannot decrypt secret LUT — wrong master passphrase or corrupt file")
    return json.loads(pt)


def save_lut(path: str | Path, passphrase: str, lut: dict) -> None:
    salt = os.urandom(16)
    token = Fernet(_key(passphrase, salt)).encrypt(json.dumps(lut).encode())
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"salt": salt.hex(), "data": token.decode()}))
    os.chmod(p, 0o600)


# ── software confirm token (sensitive-action second factor) ──────────────────────
def confirm_token(passphrase: str) -> str:
    """One-way token derived from the master; supply this at the API to confirm sensitive actions."""
    return hashlib.sha256((_CONFIRM_LABEL + passphrase).encode()).hexdigest()


def verify_confirm(passphrase: str, supplied: str | None) -> bool:
    return hmac.compare_digest(confirm_token(passphrase), supplied or "")


def make_confirm_verifier(passphrase: str):
    """Returns confirm_verifier(device_id, pin) -> bool for the control API (server/api/control.py).
    (device_id is accepted for a future per-device PIN; today one master-derived token covers all.)"""
    return lambda device_id, pin: verify_confirm(passphrase, pin)


# ── API admin bearer token (gates who may issue ANY command) ─────────────────────
def api_token(passphrase: str) -> str:
    """The control-API admin bearer = SHA256("ha-api:"+master). A SEPARATE one-way token from the
    confirm token (different label) so leaking the bearer never reveals the master nor the confirm token,
    yet there is still only ONE secret to manage (Hugh's "two separate entities" via SHA, 2026-06-21).
    Send as `Authorization: Bearer <api_token>`. The confirm token stays the second factor for sensitive
    actions, so a sniffed bearer (pre-TLS) still can't unlock anything."""
    return hashlib.sha256((_API_LABEL + passphrase).encode()).hexdigest()


def verify_api_token(passphrase: str, authorization: str | None) -> bool:
    """Accepts the raw token or a full `Bearer <token>` header value; constant-time compare."""
    supplied = authorization or ""
    if supplied.startswith("Bearer "):
        supplied = supplied[len("Bearer "):].strip()
    return hmac.compare_digest(api_token(passphrase), supplied)


def make_api_token_verifier(passphrase: str):
    """Returns authz(authorization_header) -> bool for the control router's auth dependency."""
    return lambda authorization: verify_api_token(passphrase, authorization)
