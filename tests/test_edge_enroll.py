"""ADR-0036 Layer 3 — the TOFU-lock on node-born secret claims (server/control/edge_enroll.py).

The security property under test: **first claim of a node_id binds it.** A later claim presenting a
different secret or a different MAC must be REFUSED, never silently applied. Without that lock, anyone
able to publish an enroll reply on the bus could repoint an existing node's identity at hardware they
control — which is exactly the "fake node" attack the ADR calls out as the real risk (as opposed to
secret-value collision, which at 256 bits is not a concern).

The unsigned-first-contact hole is bounded by two latches; this file covers the dictator-side one. The
node-side one-shot lives in firmware (ha_config_is_claimed / ha_config_mark_claimed).
"""
import tempfile
from pathlib import Path

from server.control import edge_enroll as EE
from server.control import secret_store as ss
from tests._harness import raises, run_module

MASTER = "test-master-passphrase"
S1 = "a" * 64
S2 = "b" * 64
MAC1 = "A0:F2:62:85:B4:14"
MAC2 = "58:E6:C5:1A:E6:BC"


def _lut_path(tmp: str) -> Path:
    return Path(tmp) / "node_secrets.enc"


def test_first_claim_enrolls():
    with tempfile.TemporaryDirectory() as tmp:
        p = _lut_path(tmp)
        changed, msg = EE.register_claim(p, MASTER, "standby_c6", MAC1, S1)
        assert changed is True, msg
        lut = ss.load_lut(p, MASTER)
        assert lut["standby_c6"]["cmd_secret"] == S1
        assert lut["standby_c6"]["mac"] == MAC1
        # Marked as node-born so these are distinguishable from build-time enrolments in the LUT.
        assert lut["standby_c6"]["provisioning"] == "node-born"
        # No mqtt_pass: inventing broker creds the node was never told would brick it at the auth cutover.
        assert "mqtt_pass" not in lut["standby_c6"]


def test_reclaim_same_secret_is_idempotent():
    """A retry after a dropped reply must not read as failure — and must not churn the LUT."""
    with tempfile.TemporaryDirectory() as tmp:
        p = _lut_path(tmp)
        EE.register_claim(p, MASTER, "standby_c6", MAC1, S1)
        changed, msg = EE.register_claim(p, MASTER, "standby_c6", MAC1, S1)
        assert changed is False, msg
        assert ss.load_lut(p, MASTER)["standby_c6"]["cmd_secret"] == S1


def test_tofu_lock_rejects_different_secret():
    """THE lock. A second claim of a bound node_id with a new secret is refused, LUT untouched."""
    with tempfile.TemporaryDirectory() as tmp:
        p = _lut_path(tmp)
        EE.register_claim(p, MASTER, "standby_c6", MAC1, S1)
        with raises(EE.ClaimError):
            EE.register_claim(p, MASTER, "standby_c6", MAC1, S2)
        assert ss.load_lut(p, MASTER)["standby_c6"]["cmd_secret"] == S1, "LUT must be unchanged"


def test_tofu_lock_rejects_different_mac():
    """Same node_id, same-looking claim, different hardware -> refuse (identity is bound to the chip)."""
    with tempfile.TemporaryDirectory() as tmp:
        p = _lut_path(tmp)
        EE.register_claim(p, MASTER, "standby_c6", MAC1, S1)
        with raises(EE.ClaimError):
            EE.register_claim(p, MASTER, "standby_c6", MAC2, S1)
        assert ss.load_lut(p, MASTER)["standby_c6"]["mac"] == MAC1


def test_other_nodes_untouched_by_a_rejected_claim():
    with tempfile.TemporaryDirectory() as tmp:
        p = _lut_path(tmp)
        EE.register_claim(p, MASTER, "node_a", MAC1, S1)
        EE.register_claim(p, MASTER, "node_b", MAC2, S2)
        with raises(EE.ClaimError):
            EE.register_claim(p, MASTER, "node_a", MAC1, S2)
        lut = ss.load_lut(p, MASTER)
        assert lut["node_a"]["cmd_secret"] == S1
        assert lut["node_b"]["cmd_secret"] == S2


def test_rejects_malformed_inputs():
    with tempfile.TemporaryDirectory() as tmp:
        p = _lut_path(tmp)
        with raises(EE.ClaimError):
            EE.register_claim(p, MASTER, "Bad-Node-ID", MAC1, S1)     # not a slug
        with raises(EE.ClaimError):
            EE.register_claim(p, MASTER, "ok_node", MAC1, "tooshort")  # not 64 hex
        with raises(EE.ClaimError):
            EE.register_claim(p, MASTER, "ok_node", MAC1, "Z" * 64)    # not hex
        assert ss.load_lut(p, MASTER) == {}


def test_validate_reply_guards():
    """A reply is attacker-influenced input: it must match the node we asked and carry a real secret."""
    assert EE._validate_reply("standby_c6", {"node": "standby_c6", "mac": MAC1, "secret": S1}) == (MAC1, S1)
    # lowercase mac normalises to upper so LUT comparisons can't drift on case
    assert EE._validate_reply("n1", {"node": "n1", "mac": MAC1.lower(), "secret": S1})[0] == MAC1
    with raises(EE.ClaimError):
        EE._validate_reply("standby_c6", {"node": "someone_else", "mac": MAC1, "secret": S1})
    with raises(EE.ClaimError):
        EE._validate_reply("n1", {"node": "n1", "mac": MAC1, "secret": "nope"})
    with raises(EE.ClaimError):
        EE._validate_reply("n1", {"node": "n1", "mac": "not-a-mac", "secret": S1})
    with raises(EE.ClaimError):
        EE._validate_reply("n1", ["not", "a", "dict"])
    # mac is optional (a node that couldn't read it still enrolls); secret never is
    assert EE._validate_reply("n1", {"node": "n1", "secret": S1}) == ("", S1)


def test_claim_never_returns_the_secret():
    """Belt-and-braces: the secret belongs in the LUT, not in an API response or a log line."""
    import inspect
    src = inspect.getsource(EE.claim_node)
    tail = src.split("return {")[-1]
    assert "secret" not in tail, "claim_node's return payload must not carry the secret"


if __name__ == "__main__":
    run_module(globals())
