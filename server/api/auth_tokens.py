"""R9 token auth (ADR-0017) — stateless HS256 JWTs with roles + expiry + rotation. Pure stdlib, no I/O
except the optional key loader; fully unit-testable. Wiring (login route, dual-verify with the legacy
bearer, route role-gates) lives in main.py — this module is just the mechanism.

A token is a compact JWT: base64url(header).base64url(payload).base64url(HMAC-SHA256(signing_key, …)).
Claims: {sub, role, iat, exp, jti}. Rotation = swap the signing_key (every old token then fails verify).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

# Role hierarchy — a higher tier satisfies any lower requirement.
ROLES = {"viewer": 0, "operator": 1, "admin": 2}
DEFAULT_TTL_S = 12 * 3600


class TokenError(Exception):
    """Verification failed; .reason is a stable short code (bad-format|bad-alg|bad-sig|expired|bad-claims)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def role_allows(have: str | None, need: str) -> bool:
    """Does role `have` satisfy a requirement of `need`? Unknown/none roles never satisfy."""
    if have not in ROLES or need not in ROLES:
        return False
    return ROLES[have] >= ROLES[need]


def mint_token(role: str, signing_key: str, *, sub: str = "user", ttl_s: int = DEFAULT_TTL_S,
               now: float | None = None, jti: str | None = None) -> str:
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    iat = int(now if now is not None else time.time())
    payload = {"sub": sub, "role": role, "iat": iat, "exp": iat + int(ttl_s),
               "jti": jti or secrets.token_hex(8)}
    seg = _b64u(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode()) + "." + \
        _b64u(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(signing_key.encode(), seg.encode(), hashlib.sha256).digest()
    return f"{seg}.{_b64u(sig)}"


def verify_token(token: str, signing_key: str, *, now: float | None = None) -> dict:
    """Return the validated claims dict, or raise TokenError. Checks alg, signature (constant-time), exp."""
    try:
        h_b64, p_b64, sig_b64 = token.split(".")
    except ValueError:
        raise TokenError("bad-format")
    try:
        header = json.loads(_b64u_dec(h_b64))
        claims = json.loads(_b64u_dec(p_b64))
    except Exception:
        raise TokenError("bad-format")
    if header.get("alg") != "HS256":                       # pin the algorithm (no 'none', no RS/HS confusion)
        raise TokenError("bad-alg")
    expected = hmac.new(signing_key.encode(), f"{h_b64}.{p_b64}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64u_dec(sig_b64), expected):
        raise TokenError("bad-sig")
    if claims.get("role") not in ROLES or "exp" not in claims:
        raise TokenError("bad-claims")
    if int(now if now is not None else time.time()) >= int(claims["exp"]):
        raise TokenError("expired")
    return claims


def bearer_role(authorization: str | None, signing_key: str, *, now: float | None = None) -> str | None:
    """Convenience for routes: extract+verify a 'Bearer <jwt>' header, returning its role or None."""
    if not authorization:
        return None
    tok = authorization[7:].strip() if authorization.startswith("Bearer ") else authorization.strip()
    try:
        return verify_token(tok, signing_key, now=now)["role"]
    except TokenError:
        return None


def load_or_create_key(path) -> str:
    """The HS256 signing key (instance/auth_key), separate from the master. Created (0600) if absent.
    Rotation = delete/overwrite this file + restart (all live tokens then fail verify)."""
    from pathlib import Path
    p = Path(path)
    if p.exists():
        return p.read_text().strip()
    key = secrets.token_hex(32)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(key)
    os.chmod(p, 0o600)
    return key
