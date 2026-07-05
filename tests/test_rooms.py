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


if __name__ == "__main__":
    from tests._harness import run_module
    raise SystemExit(run_module(globals()))
