"""Migration pending-hold: control_store accessors + viewmodel exclusion + the sweeper's fresh/expired/hold
resolution. (The device_push wiring is a thin call to store.set_pending; covered by these units.)"""
import sqlite3
from datetime import datetime, timezone

from server.api import viewmodel as V
from server.control import control_store as store
from server.maintenance import pending_sweeper as PS


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cc():
    c = sqlite3.connect(":memory:"); store.ensure_schema(c); return c


def _hot(device_id=None, last_ts=None):
    h = sqlite3.connect(":memory:")
    h.execute("CREATE TABLE device_last_seen (device_id TEXT PRIMARY KEY, last_ts TEXT)")
    h.execute("CREATE TABLE readings (device_id TEXT, ts TEXT)")
    if device_id and last_ts:
        h.execute("INSERT INTO device_last_seen VALUES (?,?)", (device_id, last_ts))
    h.commit(); return h


def test_set_get_clear_pending():
    c = _cc()
    until = store.set_pending(c, "d1", 6)
    assert store.get_device_meta(c, "d1")["pending_until"] == until
    store.clear_pending(c, "d1")
    assert store.get_device_meta(c, "d1")["pending_until"] is None


def test_pending_active_helper():
    now = 1000.0
    assert V._pending_active({"pending_until": _iso(now + 100)}, now) is True     # future → held
    assert V._pending_active({"pending_until": _iso(now - 100)}, now) is False     # past → not held
    assert V._pending_active({}, now) is False
    assert V._pending_active({"pending_until": "garbage"}, now) is False


def test_sweep_clears_hold_when_device_is_fresh():
    now = 2000.0
    c = _cc(); store.set_pending(c, "d1", 6)                                       # active hold
    h = _hot("d1", _iso(now - 60))                                                 # reported 1 min ago
    r = PS.sweep(c, h, now=now)
    assert r["cleared"] == 1 and store.get_device_meta(c, "d1")["pending_until"] is None


def test_sweep_drops_when_expired_and_stale_keeping_history():
    now = 2000.0
    c = _cc(); store.set_device_meta(c, "d1", pending_until=_iso(now - 100))       # already expired
    h = _hot("d1", _iso(now - 999999))                                             # ancient / no fresh data
    r = PS.sweep(c, h, now=now)
    m = store.get_device_meta(c, "d1")
    assert r["dropped"] == 1 and m["retired"] is True and m["pending_until"] is None   # hidden, history kept


def test_sweep_leaves_active_hold_alone():
    now = 2000.0
    c = _cc(); store.set_pending(c, "d1", 6)                                       # active, not expired
    h = _hot("d1", _iso(now - 999999))                                            # stale, but window open
    r = PS.sweep(c, h, now=now)
    assert r["kept"] == 1 and store.get_device_meta(c, "d1")["pending_until"] is not None


# ── ADR-0028 drop cleanup: LWT-clear + tasmota-devices.yaml deregister folded into the drop ──
from server.maintenance import device_push as DP


def _tas_yaml(tmp_path):
    p = tmp_path / "tasmota-devices.yaml"
    p.write_text("plug_x:\n  device_id: plug_x\n  area: office\n  device_type: power_plug\n"
                 "keep_y:\n  device_id: keep_y\n  area: shop\n  device_type: energy_meter\n")
    return p


def test_sweep_returns_dropped_ids():
    now = 2000.0
    c = _cc(); store.set_device_meta(c, "ghost", pending_until=_iso(now - 100))    # expired
    h = _hot("ghost", _iso(now - 999999))                                          # stale
    r = PS.sweep(c, h, now=now)
    assert r["dropped_ids"] == ["ghost"]                                           # drives main()'s cleanup


def test_deregister_tasmota_removes_only_that_entry(tmp_path):
    p = _tas_yaml(tmp_path)
    assert DP.deregister_tasmota("plug_x", path=p) == "plug_x"
    import yaml
    data = yaml.safe_load(p.read_text())
    assert "plug_x" not in data and "keep_y" in data                              # surgical removal


def test_deregister_tasmota_dry_run_is_noop(tmp_path):
    p = _tas_yaml(tmp_path)
    assert DP.deregister_tasmota("plug_x", dry_run=True, path=p) == "plug_x"       # reports the key
    import yaml
    assert "plug_x" in yaml.safe_load(p.read_text())                              # but changes nothing


def test_drop_cleanup_builds_state_and_lwt_topics(tmp_path):
    p = _tas_yaml(tmp_path)
    rep = DP.drop_cleanup("plug_x", dry_run=True, tas_path=p)                      # dry → no MQTT/yaml write
    assert rep["retained_cleared"] == ["home/office/plug_x/state", "tele/plug_x/LWT"]
    assert rep["deregistered"] == "plug_x"
