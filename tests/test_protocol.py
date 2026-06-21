"""Tests for the command authentication handshake (server/control/protocol.py).

These are the teeth behind 'is there a security handshake?': a node holding the per-device secret
accepts genuine commands and refuses forged / tampered / stale / replayed ones.
"""
from server.control import protocol as P
from tests._harness import run_module

SECRET = "node-c6-bench-secret-0xDEADBEEF"
T0 = 1_780_000_000.0   # fixed clock for determinism


def _cmd(**over):
    base = dict(device="lock_front", node="c6-bench", trait="lockable", action="unlock",
                args={}, secret=SECRET, sensitive=True, ts=T0)
    base.update(over)
    return P.build_command(**base)


def test_genuine_command_verifies():
    cmd = _cmd()
    v = P.Verifier(SECRET)
    ok, reason = v.verify(cmd, now=T0 + 1)
    assert ok and reason == "ok", reason


def test_forged_secret_rejected():
    cmd = _cmd()
    v = P.Verifier("attacker-guessed-secret")
    ok, reason = v.verify(cmd, now=T0 + 1)
    assert not ok and reason == "bad-sig", reason


def test_tampered_args_rejected():
    cmd = _cmd(trait="switchable", action="set", args={"on": False}, sensitive=False)
    # attacker flips the payload after signing
    cmd["args"] = {"on": True}
    v = P.Verifier(SECRET)
    ok, reason = v.verify(cmd, now=T0 + 1)
    assert not ok and reason == "bad-sig", reason


def test_stale_command_rejected():
    cmd = _cmd()
    v = P.Verifier(SECRET, max_age_s=30)
    ok, reason = v.verify(cmd, now=T0 + 120)   # 2 min later
    assert not ok and reason == "stale", reason


def test_sensitive_replay_rejected():
    cmd = _cmd()                               # unlock = sensitive
    v = P.Verifier(SECRET)
    ok1, _ = v.verify(cmd, now=T0 + 1)
    ok2, reason2 = v.verify(cmd, now=T0 + 2)   # same nonce again
    assert ok1 and not ok2 and reason2 == "replay", (ok1, ok2, reason2)


def test_nonsensitive_not_nonce_gated():
    # a non-sensitive command may legitimately repeat (idempotent set); freshness+sig still apply
    cmd = _cmd(trait="switchable", action="set", args={"on": True}, sensitive=False)
    v = P.Verifier(SECRET)
    ok1, _ = v.verify(cmd, now=T0 + 1)
    ok2, _ = v.verify(cmd, now=T0 + 2)
    assert ok1 and ok2


def test_version_mismatch_rejected():
    cmd = _cmd()
    cmd["v"] = 999
    v = P.Verifier(SECRET)
    ok, reason = v.verify(cmd, now=T0 + 1)
    assert not ok and reason == "bad-version", reason


def test_ota_directive_genuine_and_image_match():
    img = b"\x00\x01firmware-bytes\xff" * 1000
    sha = P.image_sha256(img)
    d = P.build_ota_directive(node="c6-bench", url="http://192.168.0.112:8090/fw.bin",
                              sha256_hex=sha, version=5, secret=SECRET, ts=T0)
    # 1) directive signature authenticates the directive
    ok, reason = P.Verifier(SECRET).verify(d, now=T0 + 1)
    assert ok and reason == "ok", reason
    # 2) downloaded image must match the signed hash + not be a downgrade
    ok2, r2 = P.check_ota_image(d, P.image_sha256(img), current_version=1)
    assert ok2 and r2 == "ok", r2


def test_ota_forged_directive_rejected():
    img = b"x" * 100
    d = P.build_ota_directive(node="c6-bench", url="http://evil/fw.bin",
                              sha256_hex=P.image_sha256(img), version=5, secret=SECRET, ts=T0)
    ok, reason = P.Verifier("not-the-secret").verify(d, now=T0 + 1)
    assert not ok and reason == "bad-sig", reason


def test_ota_substituted_image_rejected():
    good = b"good-firmware" * 100
    evil = b"evil-firmware" * 100
    d = P.build_ota_directive(node="c6-bench", url="http://x/fw.bin",
                              sha256_hex=P.image_sha256(good), version=5, secret=SECRET, ts=T0)
    # directive signature is valid, but the bytes that arrived are different
    assert P.Verifier(SECRET).verify(d, now=T0 + 1)[0]
    ok, reason = P.check_ota_image(d, P.image_sha256(evil), current_version=1)
    assert not ok and reason == "image-hash-mismatch", reason


def test_ota_downgrade_refused():
    img = b"older" * 100
    d = P.build_ota_directive(node="c6-bench", url="http://x/fw.bin",
                              sha256_hex=P.image_sha256(img), version=3, secret=SECRET, ts=T0)
    ok, reason = P.check_ota_image(d, P.image_sha256(img), current_version=5)
    assert not ok and reason == "downgrade-refused", reason


def test_ota_replay_rejected():
    img = b"fw" * 100
    d = P.build_ota_directive(node="c6-bench", url="http://x/fw.bin",
                              sha256_hex=P.image_sha256(img), version=9, secret=SECRET, ts=T0)
    v = P.Verifier(SECRET)
    assert v.verify(d, now=T0 + 1)[0]
    ok, reason = v.verify(d, now=T0 + 2)     # same nonce → replay
    assert not ok and reason == "replay", reason


def test_ack_shape():
    a = P.build_ack(cmd_id="abc", status="ok", reported_state={"locked": False}, ts=T0)
    assert a["id"] == "abc" and a["status"] == "ok" and a["source"] == "commanded"
    assert a["reported_state"] == {"locked": False}


if __name__ == "__main__":
    run_module(globals())
