"""Dehumidifier study Phase 3 rotation (server/control/study_rotation.py; study doc §3D): random arm order per cycle,
90-min blocks driven through the override table, RH / bucket guardrails, operator presses release a device, pause
hands everything back, and the ERV's dry boost is held outside arm E."""
import json
import math
import random
import sqlite3

import pytest

from server.control import control_store as store
from server.control import controller as C
from server.control import study_rotation as R
from server.control.issuer import Result
from server.control.registry import parse_control_registry

yaml = pytest.importorskip("yaml")
NOW = 2_000_000.0
MIDEA, APR, ERV = "dehumidifier_living_room", "dehum_attic", "erv_attic"


# ── pure planning ────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_cycle_is_every_arm_once_in_random_order():
    st, seen = {}, []
    rng = random.Random(1)
    for _ in range(len(R.ARMS)):
        arm, st = R.next_arm(st, rng, e_ok=True, bucket_full=False)
        seen.append(arm)
    assert sorted(seen) == sorted(R.ARMS) and st["cycle"] == 1
    arm, st = R.next_arm(st, rng, True, False)
    assert st["cycle"] == 2


def test_ineligible_erv_and_full_bucket_are_skipped():
    st, rng = {}, random.Random(2)
    arms = []
    for _ in range(3):
        arm, st = R.next_arm(st, rng, e_ok=False, bucket_full=True)
        arms.append(arm)
    assert set(arms) == {"0", "A"} or set(arms) <= {"0", "A"}
    assert "E" not in arms and not set(arms) & set(R.MIDEA_ARMS)


def test_forced_arm_goes_first_without_consuming_the_cycle():
    st = {"cycle": 1, "order": ["0", "M", "A", "MA", "E"], "idx": 1, "forced": "M"}
    arm, st2 = R.next_arm(st, random.Random(0), True, False)
    assert arm == "M" and st2["idx"] == 1 and "forced" not in st2


def test_guards():
    cfg = R.config(None)
    assert R.guard("0", 48.5, False, cfg) == ("rh_high", "M")
    assert R.guard("M", 48.5, False, cfg) == ("rh_high", "MA")
    assert R.guard("MA", 49.0, False, cfg) is None              # already drying with both
    assert R.guard("A", 34.0, False, cfg) == ("rh_low", "0")
    assert R.guard("MA", 44.0, True, cfg) == ("bucket_full", None)
    assert R.guard("A", None, False, cfg) is None


def test_set_config_validation():
    conn = sqlite3.connect(":memory:")
    store.ensure_schema(conn)
    assert R.set_config(conn, store, {"enabled": "yes"})[0] == 400
    assert R.set_config(conn, store, {"rh_high": 90})[0] == 400
    assert R.set_config(conn, store, {"rh_low": 47, "rh_high": 46})[0] == 400
    code, body = R.set_config(conn, store, {"enabled": True})
    assert code == 200 and R.config(store.get_setting(conn, R.SETTING))["enabled"] is True


# ── the runner against control.db ────────────────────────────────────────────────────────────────────────────────
def _db(enabled=True, **cfg):
    conn = sqlite3.connect(":memory:")
    store.ensure_schema(conn)
    store.set_setting(conn, R.SETTING, {"enabled": enabled, **cfg})
    return conn


def _rd(rh=44.0, e_ok=True, bucket=False):
    return lambda cfg: {"rh": rh, "dp_in": 9.0, "dp_out": 5.0, "t_out": 10.0, "e_ok": e_ok, "bucket_full": bucket}


def _ov(conn, dev):
    r = conn.execute("SELECT action, expiry FROM override WHERE device_id=?", (dev,)).fetchone()
    return r


def test_block_writes_its_arm_as_overrides_and_rotates(tmp_path):
    conn, rot = _db(), R.StudyRotation(store, random.Random(3))
    arm = rot.tick(conn, NOW, _rd())
    want = R.ARMS[arm]
    for role, dev in R.DEFAULTS["devices"].items():
        row = _ov(conn, dev)
        assert (row[0] if row else None) == want[role]
        if row:
            assert row[1] == NOW + 90 * 60 + 10 * 60          # block end + grace
    assert rot.tick(conn, NOW + 600, _rd()) == arm              # mid-block: same arm
    nxt = rot.tick(conn, NOW + 90 * 60 + 1, _rd())
    rows = conn.execute("SELECT arm, end_reason FROM study_block ORDER BY id").fetchall()
    assert rows[0] == (arm, "complete") and rows[1][0] == nxt and rows[1][1] is None


def test_rh_guard_ends_block_and_forces_the_cheapest_drying_arm():
    conn = _db()
    store.set_setting(conn, R.STATE, {"cycle": 1, "order": ["0", "M", "A", "MA", "E"], "idx": 0})
    rot = R.StudyRotation(store, random.Random(0))
    assert rot.tick(conn, NOW, _rd()) == "0"
    assert rot.tick(conn, NOW + 300, _rd(rh=48.2)) == "M"
    assert conn.execute("SELECT end_reason FROM study_block WHERE arm='0'").fetchone()[0] == "rh_high"
    assert _ov(conn, MIDEA)[0] == "boost_on"


def test_operator_override_releases_the_device():
    conn = _db()
    store.set_setting(conn, R.STATE, {"cycle": 1, "order": ["A", "0", "M", "MA", "E"], "idx": 0})
    rot = R.StudyRotation(store, random.Random(0))
    assert rot.tick(conn, NOW, _rd()) == "A"
    store.set_override(conn, APR, "off", NOW + 4 * 3600)          # a PWA press: off for 4 h
    rot.tick(conn, NOW + 60, _rd())
    assert APR in rot.released and _ov(conn, APR) == ("off", NOW + 4 * 3600)
    rot.tick(conn, NOW + 91 * 60, _rd())                          # next block: the press is still running
    assert _ov(conn, APR) == ("off", NOW + 4 * 3600)
    assert APR in (json.loads(conn.execute("SELECT json FROM house_setting WHERE key=?",
                                           (R.STATE,)).fetchone()[0])["block"]["released"])


def test_pause_hands_everything_back():
    conn = _db()
    rot = R.StudyRotation(store, random.Random(4))
    rot.tick(conn, NOW, _rd())
    R.set_config(conn, store, {"paused": True})
    assert rot.tick(conn, NOW + 60, _rd()) is None
    for dev in (MIDEA, APR, ERV):
        assert _ov(conn, dev) is None
    assert conn.execute("SELECT end_reason FROM study_block").fetchone()[0] == "paused"


def test_outage_longer_than_grace_marks_the_block_gap():
    conn = _db()
    rot = R.StudyRotation(store, random.Random(5))
    rot.tick(conn, NOW, _rd())
    rot.tick(conn, NOW + 3 * 3600, _rd())
    assert conn.execute("SELECT end_reason FROM study_block ORDER BY id").fetchone()[0] == "gap"


def test_erv_override_from_arm_e_is_cleared_in_the_next_arm():
    conn = _db()
    store.set_setting(conn, R.STATE, {"cycle": 1, "order": ["E", "0", "M", "A", "MA"], "idx": 0})
    rot = R.StudyRotation(store, random.Random(0))
    assert rot.tick(conn, NOW, _rd()) == "E" and _ov(conn, ERV)[0] == "boost_on"
    assert rot.tick(conn, NOW + 90 * 60, _rd()) == "0"
    assert _ov(conn, ERV) is None


# ── in the controller: the ERV's dry boost is held outside arm E ─────────────────────────────────────────────────
class Iss:
    def __init__(self):
        self.calls = []

    def issue(self, *, device_id, trait, action, args, **kw):
        self.calls.append((device_id, trait, args))
        return Result("ok", "ok", intended=args, reported=args)


class _Msg:
    def __init__(self, did, metrics):
        self.payload = json.dumps({"device_id": did, "metrics": metrics}).encode()
        self.topic = f"home/attic/{did}/state"


def test_study_holds_the_dry_boost_outside_arm_e(tmp_path):
    db = str(tmp_path / "control.db")
    conn = sqlite3.connect(db)
    store.ensure_schema(conn)
    pol = {**C.ERV_POLICY, "enabled": True, "source_sensors": ["gas_a"],
           "control": {**C.ERV_POLICY["control"], "bands": [{"max": 60, "level": 3}, {"max": 75, "level": 2},
                                                             {"max": None, "level": 1}]},
           "dry_boost": {"outdoor_sensor": "outdoor", "indoor_sensors": ["room_a"]}}
    store.set_policy(conn, ERV, pol)
    store.set_setting(conn, R.SETTING, {"enabled": True, "devices": {"midea": "nope_m", "aprilaire": "nope_a",
                                                                     "erv": ERV}})
    store.set_setting(conn, R.STATE, {"cycle": 1, "order": ["0", "E", "M", "A", "MA"], "idx": 0})
    conn.close()
    iss = Iss()
    ctrl = C.Controller(iss, {}, parse_control_registry(yaml.safe_load(open("config-examples/control.example.yaml"))),
                        db)
    ctrl._study.rng = random.Random(0)
    ctrl._latest_stored = lambda sid, metric, n: {"m": {metric: 80.0}, "ts": n - 60}
    ctrl.on_message(None, None, _Msg(ERV, {"fan_mode": 9}))       # low = band level for AQ 80
    ctrl.readings["room_a"] = {"m": {"temperature_c": 21.0, "humidity_pct": 50.0}, "ts": NOW - 30}
    ctrl.readings["outdoor"] = {"m": {"temperature_c": 12.0, "humidity_pct": 40.0}, "ts": NOW - 30}  # much drier
    ctrl.tick(now=NOW)
    assert ctrl._study.arm == "0"
    assert [a for d, t, a in iss.calls if d == ERV] == []           # dry boost would have gone to high
    t2 = NOW + 90 * 60 + 1
    for sid in ("room_a", "outdoor"):
        ctrl.readings[sid]["ts"] = t2 - 30                            # stale readings would skip arm E
    ctrl.on_message(None, None, _Msg(ERV, {"fan_mode": 9}))
    ctrl.tick(now=t2)                                                 # arm E: the study's own boost override
    assert ctrl._study.arm == "E"
    assert [a for d, t, a in iss.calls if d == ERV][-1] == {"mode": "high"}
