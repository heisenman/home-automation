"""server/api/viewmodel.py — BME680 gas fusion: the system auto-picks a reference T/H sensor and derives a
humidity-compensated air_quality, hiding the BME's self-heated temp/RH. (Pure resolver + a tiny in-mem DB.)"""
import sqlite3

from server.api import viewmodel as V


def test_resolve_ambient_ref_picks_reliable_same_room_meter():
    gas = {"device_id": "gas_hbed", "device_type": "bme680_gas", "area": "h_bed", "metrics": {}}
    devices = [
        gas,
        {"device_id": "gas_other", "device_type": "bme680_gas", "area": "h_bed",          # another gas node
         "metrics": {"temperature_c": 30, "humidity_pct": 30}},                            #   → skipped (self-heated)
        {"device_id": "meter_kitchen", "device_type": "switchbot_meter", "area": "kitchen",
         "metrics": {"temperature_c": 22, "humidity_pct": 45}},                            # wrong area → skipped
        {"device_id": "meter_h_bed", "device_type": "switchbot_meter", "area": "h_bed",
         "metrics": {"temperature_c": 24, "humidity_pct": 41}, "ts": "t1"},
        {"device_id": "meter_pro_h_bed", "device_type": "switchbot_meter_pro", "area": "h_bed",
         "metrics": {"temperature_c": 24, "humidity_pct": 41}, "ts": "t1"},                # 'pro' preferred
    ]
    assert V._resolve_ambient_ref(gas, devices)["device_id"] == "meter_pro_h_bed"


def test_resolve_ambient_ref_none_when_no_same_room_th():
    gas = {"device_id": "gas_hbed", "device_type": "bme680_gas", "area": "attic", "metrics": {}}
    devices = [gas, {"device_id": "m", "device_type": "switchbot_meter", "area": "attic",
                     "metrics": {"temperature_c": 20}}]                # humidity missing → not usable
    assert V._resolve_ambient_ref(gas, devices) is None


def _hot_with_gas_history(values):
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE readings (device_id TEXT, metric TEXT, value REAL, ts TEXT, authoritative INT)")
    for i, v in enumerate(values):
        c.execute("INSERT INTO readings VALUES('gas_hbed','gas_ohm',?,?,1)", (v, f"2026-07-08T16:00:{i:02d}Z"))
    c.commit()
    return c


def test_compensation_hides_selfheat_and_derives_air_quality():
    hot = _hot_with_gas_history([9000, 9500, 100000, 9200])           # 100k = clean-air spike → baseline
    devices = [
        {"device_id": "gas_hbed", "device_type": "bme680_gas", "area": "h_bed",
         "metrics": {"temperature_c": 36.0, "humidity_pct": 25.0, "pressure_hpa": 1017.0,
                     "gas_ohm": 9200, "dewpoint_c": 12.0}, "graphs": []},
        {"device_id": "meter_pro_h_bed", "device_type": "switchbot_meter_pro", "area": "h_bed",
         "metrics": {"temperature_c": 24.1, "humidity_pct": 41.0}, "ts": "t1"},
    ]
    V._unify_gas_nodes(devices, hot)
    gm = devices[0]["metrics"]
    assert "temperature_c" not in gm and "humidity_pct" not in gm and "dewpoint_c" not in gm  # self-heat hidden
    assert "pressure_hpa" in gm and "gas_ohm" in gm                    # kept
    assert 0 <= gm["air_quality"] <= 100                              # derived
    assert devices[0]["ambient_ref"] == "meter_pro_h_bed"            # auto-picked, transparent


def test_compensation_still_hides_bad_th_without_a_reference():
    hot = _hot_with_gas_history([9000])
    devices = [{"device_id": "gas_hbed", "device_type": "bme680_gas", "area": "nowhere",
                "metrics": {"temperature_c": 36.0, "humidity_pct": 25.0, "gas_ohm": 9000}, "graphs": []}]
    V._unify_gas_nodes(devices, hot)
    gm = devices[0]["metrics"]
    assert "temperature_c" not in gm and "humidity_pct" not in gm     # never show a self-heated number
    assert "air_quality" not in gm                                    # no reference → no fabricated index
