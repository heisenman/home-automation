"""ADR-0032 — unregistered-device data quarantine (capture + bridge hooks + merge/purge)."""
import json
import sqlite3

from server.ingest.quarantine import QuarantineSink, QuarantineStore
from server.ingest.tasmota_bridge import TasmotaBridge
from server.ingest.edge_mapper import EdgeMapper


class _Msg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()


class _Client:
    def __init__(self):
        self.pubs = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.pubs.append((topic, payload))


# ── capture-side: bridges quarantine instead of dropping ────────────────────────

def test_tasmota_unknown_is_quarantined_not_dropped(tmp_path):
    db = tmp_path / "q.db"
    sink = QuarantineSink(db)
    c = _Client()
    bridge = TasmotaBridge({}, c, quarantine=sink)          # empty registry -> everything is unknown
    bridge.on_message(c, None, _Msg("tele/pm_ghost/SENSOR",
                                    {"ENERGY": {"Power": 42.0, "Total": 1.5, "Voltage": 120.0}}))
    # nothing republished to canonical (it's unregistered) ...
    assert not any(t.startswith("home/") for t, _ in c.pubs)
    # ... but it's captured with real metrics
    store = QuarantineStore(db)
    devs = store.list_devices()
    assert len(devs) == 1 and devs[0]["identity"] == "pm_ghost" and devs[0]["source"] == "tasmota"
    s = store.summary("tasmota", "pm_ghost")
    assert s["metrics_seen"].get("power_w") == 1 and s["metrics_seen"].get("energy_kwh") == 1
    store.close()
    sink.close()


def test_known_tasmota_device_is_not_quarantined(tmp_path):
    db = tmp_path / "q.db"
    sink = QuarantineSink(db)
    c = _Client()
    reg = {"pm_real": {"device_id": "pm_real", "area": "h_office", "device_type": "energy_meter"}}
    bridge = TasmotaBridge(reg, c, quarantine=sink)
    bridge.on_message(c, None, _Msg("tele/pm_real/SENSOR", {"ENERGY": {"Power": 10.0}}))
    assert any(t == "home/h_office/pm_real/state" for t, _ in c.pubs)   # normal happy path
    assert QuarantineStore(db).list_devices() == []                    # and nothing quarantined
    sink.close()


def test_edge_unknown_mac_is_quarantined(tmp_path):
    db = tmp_path / "q.db"
    sink = QuarantineSink(db)
    c = _Client()
    mapper = EdgeMapper({}, c, quarantine=sink)
    mapper.on_message(c, None, _Msg("home/edge/c6/AA:BB:CC:00:00:09/adv",
                                    {"schema": 1, "node": "c6", "mac": "AA:BB:CC:00:00:09",
                                     "ts": "2026-07-10T00:00:00Z", "transport": "ble-adv",
                                     "metrics": {"temperature_c": 21.5}}))
    store = QuarantineStore(db)
    rows = store.readings("edge", "AA:BB:CC:00:00:09")
    assert len(rows) == 1 and rows[0]["reading_ts"] == "2026-07-10T00:00:00Z"
    assert json.loads(rows[0]["metrics_json"]) == {"temperature_c": 21.5}
    store.close()
    sink.close()


def test_empty_ts_does_not_collapse_distinct_readings(tmp_path):
    # A clockless node (E1001 SHT40) sends ts="". Distinct readings must NOT dedup onto "" — each capture
    # at a different recv_ts is its own row (else the backlog silently collapses to 1 row).
    db = tmp_path / "q.db"
    sink = QuarantineSink(db)
    for i, temp in enumerate([21.0, 21.5, 22.0]):
        sink.capture(source="edge", identity="CLOCKLESS", topic="home/edge/n/CLOCKLESS/adv",
                     payload={"ts": ""}, metrics={"temperature_c": temp}, reading_ts="",
                     recv_ts=f"2026-07-10T00:0{i}:00Z")
    assert len(QuarantineStore(db).readings("edge", "CLOCKLESS")) == 3
    sink.close()


def test_duplicate_capture_is_idempotent(tmp_path):
    db = tmp_path / "q.db"
    sink = QuarantineSink(db)
    payload = {"schema": 1, "node": "c6", "mac": "AA:BB:CC:00:00:09",
               "ts": "2026-07-10T00:00:00Z", "metrics": {"temperature_c": 21.5}}
    for _ in range(3):
        sink.capture(source="edge", identity="AA:BB:CC:00:00:09", topic="home/edge/c6/x/adv",
                     payload=payload, metrics=payload["metrics"], reading_ts=payload["ts"])
    store = QuarantineStore(db)
    assert len(store.readings("edge", "AA:BB:CC:00:00:09")) == 1   # same (identity, ts, topic) collapses
    sink.close()


# ── merge / purge side ──────────────────────────────────────────────────────────

def _hot_rows(hot_db):
    conn = sqlite3.connect(hot_db)
    try:
        return conn.execute("SELECT device_id, metric, value FROM readings ORDER BY metric").fetchall()
    finally:
        conn.close()


def test_merge_replays_into_hot_db_and_marks_merged(tmp_path):
    db = tmp_path / "q.db"
    hot = tmp_path / "hot.db"
    sink = QuarantineSink(db)
    for i, p in enumerate([20.0, 21.0]):
        sink.capture(source="edge", identity="MAC1", topic="home/edge/c6/MAC1/adv",
                     payload={"x": i}, metrics={"temperature_c": p},
                     reading_ts=f"2026-07-10T00:0{i}:00Z", transport="ble-adv")
    sink.close()

    store = QuarantineStore(db)
    rep = store.merge("edge", "MAC1", device_id="meter_new", area="office",
                      device_type="switchbot_meter", hot_db=hot)
    assert rep["ok"] and rep["readings_written"] == 2

    rows = _hot_rows(str(hot))
    assert rows == [("meter_new", "temperature_c", 20.0), ("meter_new", "temperature_c", 21.0)]

    # Hugh's directive: merged data is NOT deleted — rows persist, marked merged.
    assert store.readings("edge", "MAC1", status="merged") and not store.readings("edge", "MAC1", status="pending")
    assert store.list_devices() == []                          # merged devices drop off the pending list
    assert len(store.list_devices(include_merged=True)) == 1
    store.close()


def test_purge_requires_intent_and_deletes(tmp_path):
    db = tmp_path / "q.db"
    sink = QuarantineSink(db)
    sink.capture(source="tasmota", identity="junk", topic="tele/junk/SENSOR",
                 payload={"ENERGY": {}}, metrics={"power_w": 1.0})
    sink.close()
    store = QuarantineStore(db)
    dry = store.purge("tasmota", "junk", dry_run=True)
    assert dry["rows_deleted"] == 1 and store.readings("tasmota", "junk")   # dry-run kept it
    store.purge("tasmota", "junk")
    assert not store.readings("tasmota", "junk") and store.list_devices() == []
    store.close()


if __name__ == "__main__":
    from tests._harness import run_module
    run_module(globals())
