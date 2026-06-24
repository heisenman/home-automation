"""Web Push (payload-less tickle) — VAPID JWT signing. The strongest check: the JWT we build actually
VERIFIES against the VAPID public key, and carries the right claims. No network here."""
import base64
import json

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from server.api import webpush


def _make_vapid():
    priv = ec.generate_private_key(ec.SECP256R1())
    d = priv.private_numbers().private_value.to_bytes(32, "big")
    point = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"public": webpush._b64u(point), "private": webpush._b64u(d), "subject": "mailto:t@example.com"}, priv


def _b64u_dec(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_vapid_header_jwt_verifies_and_has_claims():
    vapid, priv = _make_vapid()
    endpoint = "https://fcm.googleapis.com/fcm/send/abc123"
    hdr = webpush.vapid_auth_header(endpoint, vapid, now=1_000_000)
    assert hdr["Authorization"].startswith("vapid t=")
    token = hdr["Authorization"].split("t=", 1)[1].split(",k=")[0]
    h_b64, p_b64, sig_b64 = token.split(".")

    header = json.loads(_b64u_dec(h_b64))
    payload = json.loads(_b64u_dec(p_b64))
    assert header == {"typ": "JWT", "alg": "ES256"}
    assert payload["aud"] == "https://fcm.googleapis.com"     # origin only, no path
    assert payload["sub"] == "mailto:t@example.com"
    assert payload["exp"] == 1_000_000 + 12 * 3600

    # the raw R||S JOSE signature must verify against the public key over "header.payload"
    raw = _b64u_dec(sig_b64)
    assert len(raw) == 64
    der = utils.encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    priv.public_key().verify(der, f"{h_b64}.{p_b64}".encode(), ec.ECDSA(hashes.SHA256()))  # raises if bad


def test_aud_is_per_endpoint_origin():
    vapid, _ = _make_vapid()
    h = webpush.vapid_auth_header("https://updates.push.services.mozilla.com/wpush/v2/xyz", vapid, now=1)
    payload = json.loads(_b64u_dec(h["Authorization"].split("t=", 1)[1].split(",k=")[0].split(".")[1]))
    assert payload["aud"] == "https://updates.push.services.mozilla.com"


def test_alert_key_stable_identity():
    a = {"kind": "low_battery", "device_id": "meter_h_bed", "detail": "battery 9%"}
    assert webpush.alert_key(a) == "low_battery:meter_h_bed"


def test_push_subscription_store_roundtrip():
    import sqlite3

    from server.control import control_store as store
    c = sqlite3.connect(":memory:")
    store.ensure_schema(c)
    store.add_push_sub(c, "https://ep/1", "p1", "a1")
    store.add_push_sub(c, "https://ep/2")
    store.add_push_sub(c, "https://ep/1", "p1b", "a1b")            # idempotent upsert on endpoint
    subs = store.all_push_subs(c)
    assert len(subs) == 2
    assert next(s for s in subs if s["endpoint"] == "https://ep/1")["p256dh"] == "p1b"
    store.remove_push_sub(c, "https://ep/1")
    assert [s["endpoint"] for s in store.all_push_subs(c)] == ["https://ep/2"]
