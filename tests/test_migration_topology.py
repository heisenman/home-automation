"""migration_topology: coverage/fallback analysis + chunked migration order (air-gap Phase 4).
Pure-function tests over constructed relay/reach maps + tmp manifests/DBs — no live broker or mesh."""
import sqlite3
from server.maintenance import migration_topology as MT


def _edge(tmp_path, nodes):
    d = tmp_path / "esp32c6"; d.mkdir()
    body = "nodes:\n" + "".join(f"  {n}:\n    mac: aa:bb\n" for n in nodes)
    (d / "nodes.yaml").write_text(body)
    return tmp_path


DEVICES = {
    "meterA":   {"type": "switchbot_meter", "area": "a", "node_id": None, "mac": "AA:AA:AA:AA:AA:AA"},
    "gas_hub":  {"type": "bme680_gas",      "area": "h", "node_id": "hub",     "mac": None},
    "gas_only": {"type": "sgp30_gas",       "area": "g", "node_id": "gasnode", "mac": None},
    "radon":    {"type": "aranet_radon_plus", "area": "cr", "node_id": None,  "mac": "BB:BB:BB:BB:BB:BB"},
}


def test_primary_fallback_and_gasonly_vs_hub(tmp_path):
    relay = {"hub": {"AA:AA:AA:AA:AA:AA", "GAS_HUB"}, "gasnode": {"GAS_ONLY"}}
    reach = {"meterA": [("hub", -60, 9.0), ("fb", -70, 6.0)],
             "gas_hub": [("hub", None, 10.0)], "gas_only": [("gasnode", None, 10.0)]}
    edge = _edge(tmp_path, ["hub", "gasnode", "fb"])
    rep = MT.analyze(DEVICES, relay, reach, edge_dir=edge)
    b = next(x for x in rep["ble"] if x["device_id"] == "meterA")
    assert b["primary"] == "hub"
    assert [n for n, _ in b["strong"]] == ["hub", "fb"]              # sorted best-first, both strong
    order = dict(MT.suggest_order(rep))
    gasonly = [t for t, _ in order["1. Gas-only C6s (BLE-negligible — safest live nodes)"]]
    hubs = [t for t, _ in order["2. BLE hubs (front-load air-gap coverage; move highest-coverage first)"]]
    assert gasonly == ["gasnode"]                                    # relays only its own gas
    assert hubs == ["hub", "fb"]                                     # hub(9.0) before fb(6.0)


def test_server_only_sensor_warns_not_unheard(tmp_path):
    reach = {"radon": [("server", -73, 6.0)]}                        # only the dictator scanner hears it
    rep = MT.analyze(DEVICES, {}, reach, edge_dir=_edge(tmp_path, []))
    w = "\n".join(rep["warnings"])
    assert "radon" in w and "server/dictator scanner" in w
    assert "server" not in rep["nodes"]                              # server is not a migratable node


def test_truly_unheard_sensor_warns(tmp_path):
    rep = MT.analyze(DEVICES, {}, {}, edge_dir=_edge(tmp_path, []))
    assert any("radon" in x and "UNHEARD" in x for x in rep["warnings"])


def test_migrated_node_detected_from_stale_household(tmp_path):
    db = tmp_path / "hot.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE device_last_seen(device_id TEXT, last_ts TEXT)")
    con.executemany("INSERT INTO device_last_seen VALUES(?,?)", [
        ("gas_hub",  "2026-07-08T22:00:00Z"),          # fresh (== newest)
        ("gas_only", "2026-07-08T20:00:00Z"),          # 120 min stale -> its node migrated
    ])
    con.commit(); con.close()
    migrated = MT.migrated_nodes(DEVICES, path=db, stale_min=15)
    assert migrated == {"gasnode"} and "hub" not in migrated


def test_migrated_node_excluded_from_order(tmp_path):
    relay = {"hub": {"AA:AA:AA:AA:AA:AA"}}
    reach = {"meterA": [("hub", -60, 9.0)]}
    rep = MT.analyze(DEVICES, relay, reach, migrated={"hub"}, edge_dir=_edge(tmp_path, ["hub"]))
    order = dict(MT.suggest_order(rep))
    assert order["0. Already migrated (off the household broker)"] == [("hub", "done")]
    assert "2. BLE hubs (front-load air-gap coverage; move highest-coverage first)" not in order
