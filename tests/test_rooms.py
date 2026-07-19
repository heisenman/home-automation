"""Tests for the canonical room graph (server/api/viewmodel.py::build_rooms).

build_rooms is pure over the sensor-list dicts (no DB), so these run offline with hand-built inputs.
Covers: every canonical area appears (incl. empty), file-order preserved, geometry + placement attach,
role split (sensor/actuator) → counts, and the `unlocated` surface for a device in a non-canonical area.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.api import viewmodel as V  # noqa: E402

AREAS = {
    "kitchen":     {"name": "Kitchen",     "level": "main_floor", "zone": "common",  "type": "kitchen"},
    "h_office":    {"name": "H Office",    "level": "main_floor", "zone": "h_suite", "type": "office"},
    "dining_nook": {"name": "Dining Nook", "level": "main_floor", "zone": "common",  "type": "dining"},
    "attic":       {"name": "Attic",       "level": "attic",      "zone": "none",    "type": "monolithic"},
}
GEO = {"rooms": {"kitchen": {"poly": [[0, 0], [1, 0], [1, 1], [0, 1]], "label": [0.5, 0.5]}}}
PLACE = {"meter_kitchen": {"x": 0.3, "y": 0.7, "anchor": "e"}}


def _sensor(did, area, dtype="switchbot_meter_outdoor", metrics=None, room=None):
    return {"device_id": did, "device_type": dtype, "area": area, "room": room or area,
            "metrics": metrics or {"temperature_c": 22.0}, "age_s": 5.0, "name": None}


def test_every_area_present_in_file_order():
    out = V.build_rooms([], AREAS)
    ids = [r["id"] for r in out["rooms"]]
    assert ids == ["kitchen", "h_office", "dining_nook", "attic"], ids
    assert [r["order"] for r in out["rooms"]] == [0, 1, 2, 3]
    # empty rooms still appear, with zeroed counts
    dn = next(r for r in out["rooms"] if r["id"] == "dining_nook")
    assert dn["devices"] == [] and dn["counts"] == {"sensors": 0, "actuators": 0, "edge": 0}


def test_levels_and_zones_deduped_ordered_no_none():
    out = V.build_rooms([], AREAS)
    assert out["levels"] == ["main_floor", "attic"]
    assert out["zones"] == ["common", "h_suite"]           # "none" excluded, dedup, file order


def test_devices_group_by_room_with_geometry_and_placement():
    sensors = [_sensor("meter_kitchen", "kitchen"), _sensor("gas_kitchen", "kitchen", dtype="sgp40_gas",
                                                             metrics={"voc_index": 185})]
    out = V.build_rooms(sensors, AREAS, geometry=GEO, placement=PLACE)
    k = next(r for r in out["rooms"] if r["id"] == "kitchen")
    assert k["geometry"]["label"] == [0.5, 0.5]
    assert {d["device_id"] for d in k["devices"]} == {"meter_kitchen", "gas_kitchen"}
    assert k["counts"] == {"sensors": 2, "actuators": 0, "edge": 0}
    mk = next(d for d in k["devices"] if d["device_id"] == "meter_kitchen")
    assert mk["placement"] == {"x": 0.3, "y": 0.7, "anchor": "e"}
    # a device with no placement gets the null default
    gk = next(d for d in k["devices"] if d["device_id"] == "gas_kitchen")
    assert gk["placement"] == {"x": None, "y": None, "anchor": "auto"}
    # a room without geometry is null, not missing
    assert next(r for r in out["rooms"] if r["id"] == "h_office")["geometry"] is None


def test_controllable_ids_mark_actuators():
    sensors = [_sensor("meter_kitchen", "kitchen"),
               _sensor("purifier_h_office", "h_office", dtype="air_purifier", metrics={"fan_on": 1})]
    out = V.build_rooms(sensors, AREAS, controllable_ids={"purifier_h_office"})
    ho = next(r for r in out["rooms"] if r["id"] == "h_office")
    assert ho["devices"][0]["role"] == "actuator"
    assert ho["counts"] == {"sensors": 0, "actuators": 1, "edge": 0}


def test_room_overlay_wins_over_registry_area():
    # user moved the device to h_office via the R8 overlay (room != area)
    out = V.build_rooms([_sensor("wanderer", "kitchen", room="h_office")], AREAS)
    assert [d["device_id"] for r in out["rooms"] if r["id"] == "h_office" for d in r["devices"]] == ["wanderer"]
    assert all(d["device_id"] != "wanderer" for r in out["rooms"] if r["id"] == "kitchen" for d in r["devices"])


def test_noncanonical_area_surfaces_as_unlocated():
    out = V.build_rooms([_sensor("stray", "garage")], AREAS)   # garage isn't in AREAS
    assert out["unlocated"] and out["unlocated"][0]["device_id"] == "stray"
    assert out["unlocated"][0]["area"] == "garage"
    # and it is NOT silently dropped into any room
    assert all(d["device_id"] != "stray" for r in out["rooms"] for d in r["devices"])


def test_empty_inputs_do_not_error():
    out = V.build_rooms([], {})
    assert out == {"schema_version": 1, "levels": [], "zones": [], "rooms": [], "unlocated": []}


# ── build_actuator_list: no controllable device silently disappears (the dehumidifier case) ──

def test_actuator_list_surfaces_readingless_actuator_from_registry():
    # the Midea dehumidifier: non-authoritative telemetry → absent from build_sensor_list; area lives in
    # the control registry (control.yaml = living_room), NOT device_last_seen (which the stale controller
    # stamped 'unknown'). build_actuator_list must still surface it, located, with no readings at all.
    reg = {"dehumidifier_living_room": {"area": "kitchen", "device_type": "dehumidifier"}}
    out = V.build_actuator_list(None, reg, present_ids=set(), meta={}, now=1000.0)
    assert len(out) == 1
    e = out[0]
    assert e["device_id"] == "dehumidifier_living_room"
    assert e["area"] == "kitchen" and e["room"] == "kitchen"   # sourced from the registry
    assert e["device_type"] == "dehumidifier" and e["metrics"] == {}


def test_actuator_list_skips_already_present_devices():
    reg = {"purifier_h_office": {"area": "h_office", "device_type": "air_purifier"}}
    out = V.build_actuator_list(None, reg, present_ids={"purifier_h_office"}, meta={})
    assert out == []                                            # already in the authoritative sensor list


def test_actuator_list_pulls_latest_metrics_any_trust_level():
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE readings(device_id TEXT, metric TEXT, value REAL, ts TEXT, authoritative INT)")
    # two non-authoritative self-reports (authoritative=0) — the sensor path would ignore these
    c.executemany("INSERT INTO readings VALUES(?,?,?,?,0)",
                  [("dehumidifier_living_room", "humidity_pct", 32.0, "2026-07-06T18:00:00Z"),
                   ("dehumidifier_living_room", "humidity_pct", 41.0, "2026-07-06T18:05:00Z"),
                   ("dehumidifier_living_room", "temperature_c", 20.0, "2026-07-06T18:05:00Z")])
    reg = {"dehumidifier_living_room": {"area": "living_room", "device_type": "dehumidifier"}}
    out = V.build_actuator_list(c, reg, present_ids=set(), meta={}, now=1000.0)
    m = out[0]["metrics"]
    assert m["humidity_pct"] == 41.0                            # latest wins
    assert m["temperature_c"] == 20.0
    assert "dewpoint_c" in m                                    # derived from temp+RH


def test_actuator_list_honours_hidden_overlay():
    reg = {"dehumidifier_living_room": {"area": "living_room", "device_type": "dehumidifier"}}
    out = V.build_actuator_list(None, reg, present_ids=set(),
                                meta={"dehumidifier_living_room": {"hidden": True}})
    assert out == []


def test_unlocated_carries_red_flag():
    # a device whose area is not canonical → surfaced in unlocated with the explicit red-flag signal
    out = V.build_rooms([_sensor("stray", "garage")], AREAS)
    u = out["unlocated"][0]
    assert u["location_unknown"] is True and "garage" in u["reason"]
    # a genuinely location-less device (area 'unknown') gets the no-location reason
    out2 = V.build_rooms([_sensor("nowhere", "unknown")], AREAS)
    assert out2["unlocated"][0]["location_unknown"] is True
    assert "no resolved location" in out2["unlocated"][0]["reason"]


def test_readingless_actuator_end_to_end_placed_or_flagged():
    # located actuator (canonical registry area) lands in its room; unknown-area one → unlocated red flag
    reg = {"dehumidifier_living_room": {"area": "kitchen", "device_type": "dehumidifier"},
           "orphan_actuator": {"area": "unknown", "device_type": "dehumidifier"}}
    acts = V.build_actuator_list(None, reg, present_ids=set(), meta={}, now=1.0)
    out = V.build_rooms(acts, AREAS, controllable_ids=set(reg))
    kitchen = next(r for r in out["rooms"] if r["id"] == "kitchen")
    assert [d["device_id"] for d in kitchen["devices"]] == ["dehumidifier_living_room"]
    assert kitchen["devices"][0]["role"] == "actuator"
    assert [u["device_id"] for u in out["unlocated"]] == ["orphan_actuator"]
    assert out["unlocated"][0]["location_unknown"] is True


# ── room-fill dev half (docs/design/map-room-fill.md): climate resolver, range flags, ordering ──

def _cs(did, temp=None, hum=None, age=5.0, role=None):
    """A room-climate sensor input dict (as build_rooms accumulates)."""
    m = {}
    if temp is not None:
        m["temperature_c"] = temp
    if hum is not None:
        m["humidity_pct"] = hum
    return {"device_id": did, "metrics": m, "age_s": age, "climate_role": role}


def test_climate_none_when_no_temperature():
    assert V.resolve_room_climate([]) is None
    assert V.resolve_room_climate([_cs("d", hum=50)]) is None      # humidity alone isn't a climate read


def test_climate_primary_role_wins_over_mean():
    out = V.resolve_room_climate([_cs("a", 21.0, 45, role="primary"), _cs("b", 25.0, 55)])
    assert out["confidence"] == "primary" and out["source_device_id"] == "a"
    assert out["value"] == {"temperature_c": 21.0, "humidity_pct": 45}


def test_climate_stale_primary_falls_to_secondary():
    out = V.resolve_room_climate([_cs("p", 21.0, 45, age=9999, role="primary"),   # stale
                                  _cs("s", 22.0, 46, role="secondary")])
    assert out["confidence"] == "secondary" and out["source_device_id"] == "s"


def test_climate_averaged_and_divergent():
    # two close sensors -> averaged (amber)
    out = V.resolve_room_climate([_cs("a", 21.0, 45), _cs("b", 22.0, 47)])
    assert out["confidence"] == "averaged" and out["source_device_id"] is None
    assert out["value"] == {"temperature_c": 21.5, "humidity_pct": 46.0}
    # a wide temperature spread -> averaged_divergent (red) — an average hiding a real spread
    div = V.resolve_room_climate([_cs("a", 18.0, 45), _cs("b", 25.0, 46)])
    assert div["confidence"] == "averaged_divergent"


def test_out_of_range_map_flags_only_out_of_range():
    m = {"temperature_c": 21.0, "humidity_pct": 80.0, "co2_ppm": 1500}   # temp ok; RH + CO2 high
    oor = V.out_of_range_map(m)
    assert oor == {"humidity_pct": True, "co2_ppm": True}


def test_build_rooms_attaches_climate_and_orders_devices():
    sensors = [_sensor("meter_kitchen", "kitchen", metrics={"temperature_c": 21.0, "humidity_pct": 44.0}),
               _sensor("gas_kitchen", "kitchen", dtype="sgp40_gas", metrics={"voc_index": 300})]
    out = V.build_rooms(sensors, AREAS, controllable_ids={"gas_kitchen"})   # pretend gas is the actuator
    k = next(r for r in out["rooms"] if r["id"] == "kitchen")
    # climate resolved from the sensor (gas is an actuator, excluded from climate)
    assert k["climate"]["confidence"] == "averaged" and k["climate"]["value"]["temperature_c"] == 21.0
    # actuator ordered before sensor
    assert [d["role"] for d in k["devices"]] == ["actuator", "sensor"]
    # out-of-range surfaced on the device (voc_index 300 > 250)
    gas = next(d for d in k["devices"] if d["device_id"] == "gas_kitchen")
    assert gas["out_of_range"] == {"voc_index": True}
    # empty room has no climate
    assert next(r for r in out["rooms"] if r["id"] == "dining_nook")["climate"] is None


def test_actuator_map_state_status_is_family_agnostic_detail():
    # dehumidifier -> target RH %
    dehum = {"running": True, "actuator": {"target_pct": 45.0}, "traits": {}}
    assert V.actuator_map_state(dehum) == {"running": True, "status": "45%"}
    # purifier, 2-speed ranged -> named level verbatim (40->Low / 80->High)
    purifier2 = {"running": True, "actuator": {"fan_speed": 80.0},
                 "traits": {"ranged": {"min": 40, "max": 80, "step": 40}}}
    assert V.actuator_map_state(purifier2) == {"running": True, "status": "High"}
    # purifier, 4-speed ranged -> numeric level gets a "Fan N" prefix (not a bare "1")
    purifier4 = {"running": True, "actuator": {"fan_speed": 1.0},
                 "traits": {"ranged": {"min": 1, "max": 4, "step": 1}}}
    assert V.actuator_map_state(purifier4) == {"running": True, "status": "Fan 1"}
    # plug/switch with no target/level -> "on" running, "" idle
    assert V.actuator_map_state({"running": True, "actuator": {}, "traits": {}})["status"] == "on"
    assert V.actuator_map_state({"running": False, "actuator": {}, "traits": {}})["status"] == ""
    # status is verbatim; running coerced to bool
    assert V.actuator_map_state({"running": None, "actuator": {}})["running"] is False


def test_build_rooms_passes_actuator_state_through():
    # the rooms_list handler attaches state {running,status} on actuators; build_rooms carries it verbatim
    a = _sensor("purifier_h_office", "h_office", dtype="air_purifier", metrics={"fan_on": 1})
    a["state"] = {"running": True, "status": "ok"}
    s = _sensor("meter_kitchen", "kitchen")               # a sensor gets no state
    out = V.build_rooms([a, s], AREAS, controllable_ids={"purifier_h_office"})
    ho = next(r for r in out["rooms"] if r["id"] == "h_office")
    assert ho["devices"][0]["state"] == {"running": True, "status": "ok"}
    kit = next(r for r in out["rooms"] if r["id"] == "kitchen")
    assert kit["devices"][0]["state"] is None


if __name__ == "__main__":
    from tests._harness import run_module
    raise SystemExit(run_module(globals()))


# ── build_panel_rooms: one-row-per-room dashboard (E1001 landscape) ─────────────────────────────────
def _s(device_id, room, **metrics):
    return {"device_id": device_id, "room": room, "area": room, "metrics": metrics}


def test_panel_rooms_fixed_columns_and_notable_tail():
    sensors = [
        _s("meter_office", "c_office", temperature_c=22.5, humidity_pct=40.0, dewpoint_c=8.2, battery_pct=95),
        _s("radon_crawl", "crawlspace", temperature_c=17.0, humidity_pct=66.0, dewpoint_c=10.9,
           radon_bqm3=38.0, pressure_hpa=1010.0),
    ]
    out = V.build_panel_rooms(sensors)
    rooms = {r["room"]: r for r in out["rooms"]}
    assert set(rooms) == {"c_office", "crawlspace"}
    # fixed climate columns present (temps stay °C — the panel converts)
    assert rooms["c_office"]["temperature_c"] == 22.5 and rooms["c_office"]["humidity_pct"] == 40
    assert rooms["c_office"]["dewpoint_c"] == 8.2
    # battery is NOT a notable extra; c_office has no other metric -> empty tail
    assert rooms["c_office"]["notable"] == []
    # crawlspace's room-specific metrics land in the free-form notable tail, labeled from METRIC_CATALOG
    notable = {n["key"]: n for n in rooms["crawlspace"]["notable"]}
    assert set(notable) == {"radon_bqm3", "pressure_hpa"}
    assert notable["radon_bqm3"]["label"] == "Radon" and notable["radon_bqm3"]["value"] == 38
    assert "temperature_c" not in notable and "humidity_pct" not in notable   # fixed cols never in tail


def test_panel_rooms_omits_rooms_without_temperature():
    # a room with only an actuator/non-temp reading has nothing to anchor a climate row -> omitted
    sensors = [_s("plug_x", "garage", fan_on=1, fan_speed=80)]
    assert V.build_panel_rooms(sensors)["rooms"] == []


def test_panel_rooms_air_band_from_worst_gas_node():
    sensors = [{"device_id": "gas_lr", "room": "living_room", "area": "living_room",
                "metrics": {"temperature_c": 23.0, "humidity_pct": 45.0},
                "air_quality_report": {"air_quality_band": 3, "air_quality_band_label": "Fair",
                                       "air_quality_basis": "absolute", "air_quality": 55}}]
    r = V.build_panel_rooms(sensors)["rooms"][0]
    assert r["air_band"] == 3 and r["air_band_label"] == "Fair"
