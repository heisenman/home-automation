"""Tests for the encrypted secret store + SHA-derived confirm token (server/control/secret_store.py)."""
import os
import tempfile

from server.control import secret_store as S
from tests._harness import raises, run_module

MASTER = "CHANGE_ME_master_passphrase"


def test_lut_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "node_secrets.enc")
        lut = {"c6-bench": {"mac": "AA:BB:CC:00:00:02", "cmd_secret": "abc123",
                            "mqtt_user": "c6-bench", "mqtt_pass": "p", "created": "2026-06-21"}}
        S.save_lut(path, MASTER, lut)
        assert oct(os.stat(path).st_mode)[-3:] == "600"        # private at rest
        assert S.load_lut(path, MASTER) == lut


def test_wrong_passphrase_fails():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "x.enc")
        S.save_lut(path, MASTER, {"n": {"cmd_secret": "s"}})
        with raises(ValueError):
            S.load_lut(path, "wrong-master")


def test_load_missing_is_empty():
    assert S.load_lut("/nonexistent/x.enc", MASTER) == {}


def test_at_rest_ciphertext_hides_secret():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "x.enc")
        S.save_lut(path, MASTER, {"n": {"cmd_secret": "TOPSECRETVALUE"}})
        blob = open(path).read()
        assert "TOPSECRETVALUE" not in blob and MASTER not in blob   # encrypted, master not stored


def test_confirm_token_one_way_and_verify():
    tok = S.confirm_token(MASTER)
    assert len(tok) == 64 and MASTER not in tok                 # SHA-256 hex, master not recoverable
    assert tok != S.confirm_token("Canticum2")                  # depends on the master
    assert S.verify_confirm(MASTER, tok) is True
    assert S.verify_confirm(MASTER, "deadbeef") is False
    assert S.verify_confirm(MASTER, None) is False


def test_confirm_verifier_for_api():
    v = S.make_confirm_verifier(MASTER)
    assert v("lock_front", S.confirm_token(MASTER)) is True
    assert v("lock_front", "nope") is False


def test_api_token_one_way_and_distinct_from_confirm():
    tok = S.api_token(MASTER)
    assert len(tok) == 64 and MASTER not in tok                 # SHA-256 hex, master not recoverable
    assert tok != S.confirm_token(MASTER)                       # different label → different token
    assert tok != S.api_token("Canticum2")                      # depends on the master


def test_api_token_verify_accepts_bearer_or_raw():
    tok = S.api_token(MASTER)
    assert S.verify_api_token(MASTER, tok) is True              # raw token
    assert S.verify_api_token(MASTER, f"Bearer {tok}") is True  # full header value
    assert S.verify_api_token(MASTER, "Bearer nope") is False
    assert S.verify_api_token(MASTER, None) is False
    assert S.verify_api_token(MASTER, S.confirm_token(MASTER)) is False   # confirm token ≠ api bearer


def test_api_token_verifier_for_router():
    authz = S.make_api_token_verifier(MASTER)
    assert authz(f"Bearer {S.api_token(MASTER)}") is True
    assert authz(None) is False


def test_available_master_optional(monkeypatch=None):
    # available_master must never raise even when nothing is configured
    import os
    saved_env = os.environ.pop("HA_MASTER_PASSPHRASE", None)
    saved_file = os.environ.pop("HA_MASTER_PASS_FILE", None)
    try:
        assert S.available_master() is None or isinstance(S.available_master(), str)
        assert S.available_master("explicit-master") == "explicit-master"
    finally:
        if saved_env is not None:
            os.environ["HA_MASTER_PASSPHRASE"] = saved_env
        if saved_file is not None:
            os.environ["HA_MASTER_PASS_FILE"] = saved_file


if __name__ == "__main__":
    run_module(globals())
