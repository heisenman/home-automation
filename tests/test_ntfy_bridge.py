"""Tests for the MQTT->ntfy alert mapping (server/notify/ntfy_bridge.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.notify.ntfy_bridge import alert_to_ntfy  # noqa: E402
from tests._harness import run_module  # noqa: E402


def test_critical_maps_to_priority_5_with_tag():
    p = alert_to_ntfy({"severity": "critical", "kind": "tank_full",
                       "device_id": "dehumidifier_office", "name": "Office Dehum",
                       "detail": "tank full — dehumidifier paused"}, "ha-alerts")
    assert p["topic"] == "ha-alerts"
    assert p["priority"] == 5
    assert p["tags"] == ["droplet"]
    assert p["title"] == "Office Dehum: tank full"
    assert "tank full" in p["message"]


def test_warning_unreachable_priority_4():
    p = alert_to_ntfy({"severity": "warning", "kind": "unreachable",
                       "device_id": "meter_x", "name": "Bath Meter", "detail": "no data for 11 min"},
                      "ha-alerts")
    assert p["priority"] == 4 and p["tags"] == ["warning"]
    assert p["title"] == "Bath Meter: unreachable"


def test_info_override_priority_2():
    p = alert_to_ntfy({"severity": "info", "kind": "override_expiring",
                       "device_id": "d", "name": "D", "detail": "off override ends in 5 min"}, "t")
    assert p["priority"] == 2 and p["tags"] == ["hourglass"]


def test_unknown_kind_and_missing_fields_fall_back_safely():
    p = alert_to_ntfy({"kind": "mystery", "device_id": "dev1"}, "t")     # no severity/name/detail
    assert p["priority"] == 2                  # missing severity -> "info" default -> 2
    assert p["tags"] == ["bell"]               # unknown kind -> default tag
    assert p["title"] == "dev1: mystery"       # name falls back to device_id
    assert p["message"] == p["title"]          # message falls back to title when detail empty


def test_unknown_severity_string_uses_priority_default_3():
    p = alert_to_ntfy({"severity": "bogus", "kind": "low_battery", "device_id": "d", "name": "D",
                       "detail": "battery 8%"}, "t")
    assert p["priority"] == 3                   # severity not in the map -> get() default 3
    assert p["tags"] == ["battery"]


if __name__ == "__main__":
    run_module(globals())
