"""Payload-less Web Push ("tickle") — server side, using ONLY the existing `cryptography` dep.

We never encrypt a push payload (RFC 8291 aes128gcm — which would need pywebpush/http_ece). Instead the
server sends an EMPTY, VAPID-signed POST to the subscription endpoint; the service worker wakes on the
`push` event and fetches /api/v1/alerts itself to render the notification. So all we must do here is sign
the VAPID JWT (RFC 8292, ES256) — which `cryptography` does natively. No new dependency, no payload crypto.

vapid.json shape (from tools/gen_vapid.py): {"public": <b64url EC point>, "private": <b64url scalar>, "subject": "mailto:..."}
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _load_priv(vapid: dict) -> ec.EllipticCurvePrivateKey:
    d = int.from_bytes(_b64u_dec(vapid["private"]), "big")
    return ec.derive_private_key(d, ec.SECP256R1())


def vapid_auth_header(endpoint: str, vapid: dict, exp_s: int = 12 * 3600, now: float | None = None) -> dict:
    """The 'Authorization: vapid t=<jwt>,k=<pub>' header for ONE push endpoint. `aud` is the endpoint's
    origin (each push service is its own audience); `exp` must be <= 24h out per RFC 8292."""
    now = int(now if now is not None else time.time())
    parts = urllib.parse.urlsplit(endpoint)
    origin = f"{parts.scheme}://{parts.netloc}"
    seg = (_b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode()) + "." +
           _b64u(json.dumps({"aud": origin, "exp": now + exp_s, "sub": vapid["subject"]},
                            separators=(",", ":")).encode()))
    der = _load_priv(vapid).sign(seg.encode(), ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der)
    sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")     # JOSE wants raw R||S, not DER
    jwt = f"{seg}.{_b64u(sig)}"
    return {"Authorization": f"vapid t={jwt},k={vapid['public']}"}


def send_tickle(endpoint: str, vapid: dict, ttl: int = 600, timeout: float = 10,
                now: float | None = None) -> int:
    """POST an empty, VAPID-signed push. Returns the HTTP status: 201 = queued; 404/410 = the subscription
    is gone (caller should prune it); other = transient. Never raises on HTTP errors."""
    headers = vapid_auth_header(endpoint, vapid, now=now)
    headers.update({"TTL": str(ttl), "Content-Length": "0", "Urgency": "normal"})
    req = urllib.request.Request(endpoint, data=b"", headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def alert_key(a: dict) -> str:
    """Stable identity for an alert (build_alerts dict) so we tickle only on NEWLY-appearing alerts."""
    return f"{a.get('kind')}:{a.get('device_id')}"
