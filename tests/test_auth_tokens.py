"""R9 token auth (ADR-0017) — HS256 JWT mint/verify, roles, expiry, rotation."""
import json
import tempfile
from pathlib import Path

from server.api import auth_tokens as at

KEY = "test-signing-key-0123456789"


def _reason(fn):
    """Run fn, return the TokenError.reason it raised (or None if it didn't raise)."""
    try:
        fn()
    except at.TokenError as e:
        return e.reason
    return None


def test_mint_then_verify_roundtrip_claims():
    t = at.mint_token("admin", KEY, sub="hugh", ttl_s=3600, now=1_000_000, jti="fixedjti")
    claims = at.verify_token(t, KEY, now=1_000_500)
    assert claims["role"] == "admin" and claims["sub"] == "hugh"
    assert claims["iat"] == 1_000_000 and claims["exp"] == 1_003_600 and claims["jti"] == "fixedjti"


def test_expired_token_rejected():
    t = at.mint_token("viewer", KEY, ttl_s=60, now=1_000_000)
    assert _reason(lambda: at.verify_token(t, KEY, now=1_000_061)) == "expired"


def test_wrong_key_fails_signature():
    t = at.mint_token("admin", KEY, now=1_000_000)
    assert _reason(lambda: at.verify_token(t, "different-key", now=1_000_001)) == "bad-sig"


def test_rotation_invalidates_old_tokens():
    t = at.mint_token("admin", KEY, now=1_000_000)
    at.verify_token(t, KEY, now=1_000_001)                  # valid under the old key
    assert _reason(lambda: at.verify_token(t, "rotated-key-9999", now=1_000_001)) == "bad-sig"


def test_tampered_payload_rejected():
    t = at.mint_token("viewer", KEY, now=1_000_000)
    h, p, s = t.split(".")
    forged = at._b64u(json.dumps({"sub": "x", "role": "admin", "iat": 1_000_000, "exp": 9_999_999_999,
                                  "jti": "x"}, separators=(",", ":")).encode())
    assert _reason(lambda: at.verify_token(f"{h}.{forged}.{s}", KEY, now=1_000_001)) == "bad-sig"


def test_alg_none_downgrade_rejected():
    payload = at._b64u(json.dumps({"role": "admin", "exp": 9_999_999_999}, separators=(",", ":")).encode())
    header = at._b64u(json.dumps({"alg": "none", "typ": "JWT"}, separators=(",", ":")).encode())
    assert _reason(lambda: at.verify_token(f"{header}.{payload}.", KEY, now=1)) == "bad-alg"


def test_role_hierarchy():
    assert at.role_allows("admin", "viewer") and at.role_allows("admin", "operator")
    assert at.role_allows("operator", "viewer") and not at.role_allows("operator", "admin")
    assert at.role_allows("viewer", "viewer") and not at.role_allows("viewer", "operator")
    assert not at.role_allows(None, "viewer") and not at.role_allows("nobody", "viewer")


def test_bearer_role_extracts_or_none():
    t = at.mint_token("operator", KEY, now=1_000_000)
    assert at.bearer_role(f"Bearer {t}", KEY, now=1_000_001) == "operator"
    assert at.bearer_role(t, KEY, now=1_000_001) == "operator"        # bare token too
    assert at.bearer_role(None, KEY) is None
    assert at.bearer_role("Bearer garbage", KEY) is None


def test_load_or_create_key_persists_and_is_stable():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "auth_key"
        k1 = at.load_or_create_key(p)
        k2 = at.load_or_create_key(p)
        assert k1 == k2 and len(k1) == 64 and (p.stat().st_mode & 0o777) == 0o600


# ── R9 dual-verify (JWT or legacy SHA bearer) ─────────────────────────────────────
def _legacy(valid_value):
    """Stub legacy verifier: True iff the header equals `valid_value`."""
    return lambda authz: authz == valid_value


def test_resolve_role_prefers_valid_jwt():
    t = at.mint_token("operator", KEY, now=1_000_000)
    assert at.resolve_role(f"Bearer {t}", KEY, _legacy("anything"), now=1_000_001) == "operator"


def test_resolve_role_falls_back_to_legacy_admin():
    assert at.resolve_role("Bearer LEGACY", KEY, _legacy("Bearer LEGACY")) == "admin"


def test_resolve_role_none_when_neither():
    assert at.resolve_role("Bearer nope", KEY, _legacy("Bearer LEGACY")) is None
    assert at.resolve_role(None, KEY, _legacy("Bearer LEGACY")) is None


def test_resolve_role_expired_jwt_falls_through_to_legacy():
    t = at.mint_token("admin", KEY, ttl_s=60, now=1_000_000)        # expired by now=...061
    # JWT invalid (expired) -> not its role; legacy header doesn't match -> None
    assert at.resolve_role(f"Bearer {t}", KEY, _legacy("X"), now=1_000_061) is None
    # but if the same request also carried a valid legacy value, legacy wins
    assert at.resolve_role("Bearer LEGACY", KEY, _legacy("Bearer LEGACY"), now=1_000_061) == "admin"


def test_refresh_keeps_role_new_expiry():
    t = at.mint_token("operator", KEY, ttl_s=60, now=1_000_000)
    role = at.bearer_role(f"Bearer {t}", KEY, now=1_000_010)
    assert role == "operator"
    fresh = at.mint_token(role, KEY, ttl_s=60, now=1_000_010)
    assert at.verify_token(fresh, KEY, now=1_000_010)["exp"] == 1_000_070   # later than the original 1_000_060
