"""apply_rename_worksheet — the end-to-end orchestrator's pure planning + validation (ADR-0026 Phase 3).

The apply/restart/sweep/verify side-effects are integration-tested on a real run; here we cover the logic
that decides WHAT happens: the plan built from a worksheet + current areas, and the pre-flight validation.
"""
from server.maintenance import apply_rename_worksheet as A


def test_load_plan_computes_changes_and_skips_noops(tmp_path):
    ws = tmp_path / "device-rename.yaml"
    ws.write_text(
        "devices:\n"
        "  meter_h_bed:\n    new_id:   meter_h_bed_closet\n    new_area: h_bed_closet\n"   # rename + move
        "  levoit_office:\n    new_id:   purifier_h_office\n    new_area: h_office\n"       # rename only
        "  meter_kitchen:\n    new_id:   meter_kitchen\n    new_area: kitchen\n")           # no-op -> skipped
    orig = A.registry_area_map
    A.registry_area_map = lambda: {"meter_h_bed": "h_bed", "levoit_office": "h_office",
                                   "meter_kitchen": "kitchen"}
    try:
        plan = A.load_plan(str(ws))
    finally:
        A.registry_area_map = orig

    by = {s["old_id"]: s for s in plan}
    assert set(by) == {"meter_h_bed", "levoit_office"}                       # no-op dropped
    assert by["meter_h_bed"] == {"old_id": "meter_h_bed", "new_id": "meter_h_bed_closet",
                                 "old_area": "h_bed", "new_area": "h_bed_closet",
                                 "id_change": True, "area_change": True}
    assert by["levoit_office"]["id_change"] and not by["levoit_office"]["area_change"]


def test_load_plan_defaults_missing_fields_to_current(tmp_path):
    ws = tmp_path / "w.yaml"
    ws.write_text("devices:\n  plug_g11:\n    new_area: h_office\n")           # new_id omitted -> keep id
    orig = A.registry_area_map
    A.registry_area_map = lambda: {"plug_g11": "mech_closet"}
    try:
        plan = A.load_plan(str(ws))
    finally:
        A.registry_area_map = orig
    assert plan == [{"old_id": "plug_g11", "new_id": "plug_g11", "old_area": "mech_closet",
                     "new_area": "h_office", "id_change": False, "area_change": True}]


def test_registry_area_map_no_none_clobber(tmp_path):
    """A node->device_id registry with no area (panel-devices.yaml) must NOT overwrite the authoritative
    area from control.yaml with None (regression: d1001_panel showed old_area=None)."""
    (tmp_path / "devices.yaml").write_text('devices:\n  "MAC":\n    device_id: meter_x\n    area: kitchen\n')
    (tmp_path / "control.yaml").write_text('devices:\n  d1001_panel:\n    area: h_office\n')
    (tmp_path / "panel-devices.yaml").write_text('d1001-beachhead:\n  device_id: d1001_panel\n')  # no area
    m = A.registry_area_map(tmp_path)
    assert m["d1001_panel"] == "h_office"       # NOT clobbered to None by panel-devices.yaml
    assert m["meter_x"] == "kitchen"


VALID = {"h_bed", "h_bed_closet", "h_office", "kitchen"}


def test_validate_plan_ok():
    plan = [{"old_id": "a", "new_id": "b", "old_area": "kitchen", "new_area": "h_office"}]
    assert A.validate_plan(plan, VALID) == []


def test_validate_plan_rejects_unknown_area():
    plan = [{"old_id": "a", "new_id": "b", "old_area": "kitchen", "new_area": "nowhere"}]
    errs = A.validate_plan(plan, VALID)
    assert len(errs) == 1 and "nowhere" in errs[0]


def test_validate_plan_rejects_id_collision():
    plan = [{"old_id": "a", "new_id": "dup", "old_area": "kitchen", "new_area": "h_office"},
            {"old_id": "b", "new_id": "dup", "old_area": "kitchen", "new_area": "h_office"}]
    errs = A.validate_plan(plan, VALID)
    assert any("collide" in e for e in errs)


if __name__ == "__main__":
    from tests._harness import run_module
    run_module(globals())
