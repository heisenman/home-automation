"""control.py device-admin maintenance handlers — the input-validation boundary (rename/relocate).

Covers the fail-fast guards that return BEFORE any registry/DB touch or job launch: the source device_id
guard (peer-SSH-injection defence), new_id hygiene, and the required relocate mode/new_area."""
from server.api import control as C


def test_rename_rejects_bad_source_device_id():
    code, payload = C.handle_device_rename('evil"; rm -rf ~; echo "', {"new_id": "meter_x"})
    assert code == 400 and "device_id" in payload["reason"]


def test_rename_rejects_bad_new_id():
    code, payload = C.handle_device_rename("meter_x", {"new_id": "Bad-Id!"})
    assert code == 400 and "new_id" in payload["reason"]


def test_relocate_requires_mode():
    code, payload = C.handle_device_relocate("meter_x", {"new_area": "kitchen"})   # mode omitted
    assert code == 400 and "mode" in payload["reason"]


def test_relocate_requires_new_area():
    code, payload = C.handle_device_relocate("meter_x", {"mode": "restamp"})       # new_area omitted
    assert code == 400 and "new_area" in payload["reason"]


def test_bad_dry_run_type():
    code, payload = C.handle_device_rename("meter_x", {"new_id": "meter_y", "dry_run": "yes"})
    assert code == 400 and "dry_run" in payload["reason"]


if __name__ == "__main__":
    from tests._harness import run_module
    run_module(globals())
