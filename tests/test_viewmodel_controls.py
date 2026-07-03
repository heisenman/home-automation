"""shared-ui-spec Phase 0 — server-authored `controls` list (viewmodel.build_controls).

Pins the contract in docs/design/shared-ui-spec.md so the PWA + D1001 panel can render controls without
re-deriving ranges/labels/admin/action client-side. Mirrors app.js OverrideControls/ManualControl."""
from server.api.viewmodel import build_controls, _ranged_options


def _by_kind(controls):
    return {c["kind"]: c for c in controls}


# ── override: always present, first, control-level action ──────────────────────────────────────────
def test_override_always_present_even_with_no_traits():
    controls = build_controls(None)
    assert controls[0]["kind"] == "override"          # always first
    ov = controls[0]
    assert ov["admin"] is True
    assert ov["action"] == {"method": "POST", "path": "/control/{id}/override"}
    labels = [p["label"] for p in ov["presets"]]
    assert labels == ["Off 1h", "Boost 1h", "Resume auto"]
    # the Off/Boost presets carry a duration; clear is advisory-flagged "only when an override is active"
    assert ov["presets"][0] == {"action": "off", "duration_min": 60, "label": "Off 1h"}
    assert ov["presets"][2]["action"] == "clear" and ov["presets"][2]["when"] == "override_active"


def test_no_traits_emits_only_override():
    assert [c["kind"] for c in build_controls({})] == ["override"]


# ── setpoint ───────────────────────────────────────────────────────────────────────────────────────
def test_setpoint_carries_range_unit_label_and_action():
    c = _by_kind(build_controls({"setpoint": {"min": 40, "max": 60, "safe_value": 50, "unit": "%"}}))["setpoint"]
    assert c["min"] == 40 and c["max"] == 60 and c["safe_value"] == 50
    assert c["label"] == "Target humidity" and c["unit"] == "%"
    assert c["now_key"] == "target_pct"
    assert c["action"] == {"method": "POST", "path": "/devices/{id}/command",
                           "trait": "setpoint", "action": "set", "arg_key": "value"}


def test_setpoint_label_derives_from_unit():
    assert _by_kind(build_controls({"setpoint": {"unit": "degC"}}))["setpoint"]["label"] == "Target temperature"
    assert _by_kind(build_controls({"setpoint": {"unit": "ppb"}}))["setpoint"]["label"] == "Setpoint"


def test_setpoint_omitted_when_not_a_trait():
    assert "setpoint" not in _by_kind(build_controls({"ranged": {"min": 1, "max": 3}}))


# ── ranged: enumeration must match app.js range()/steps() exactly ────────────────────────────────────
def test_ranged_three_levels_low_med_high():
    opts = _ranged_options({"min": 1, "max": 3})
    assert opts == [{"value": 1, "label": "Low"}, {"value": 2, "label": "Med"}, {"value": 3, "label": "High"}]


def test_ranged_two_levels_low_high():
    assert [o["label"] for o in _ranged_options({"min": 0, "max": 1})] == ["Low", "High"]


def test_ranged_inclusive_of_max_and_numeric_when_not_2_or_3():
    opts = _ranged_options({"min": 1, "max": 5})          # 5 levels → numeric labels, inclusive of max
    assert [o["value"] for o in opts] == [1, 2, 3, 4, 5]
    assert [o["label"] for o in opts] == ["1", "2", "3", "4", "5"]


def test_ranged_step_honored():
    assert [o["value"] for o in _ranged_options({"min": 0, "max": 100, "step": 25})] == [0, 25, 50, 75, 100]


def test_ranged_control_action_uses_level_arg():
    c = _by_kind(build_controls({"ranged": {"min": 1, "max": 3}}))["ranged"]
    assert c["label"] == "Fan speed" and c["now_key"] == "fan_speed"
    assert c["action"] == {"method": "POST", "path": "/devices/{id}/command",
                           "trait": "ranged", "action": "set", "arg_key": "level"}


# ── indicator ────────────────────────────────────────────────────────────────────────────────────────
def test_indicator_boolean_on_action():
    c = _by_kind(build_controls({"indicator": {}}))["indicator"]
    assert c["label"] == "LED / panel light" and c["now_key"] == "led_on"
    assert c["action"]["arg_key"] == "on" and c["action"]["path"] == "/devices/{id}/command"


# ── all traits together: order + admin-gating + per-cfg label override ───────────────────────────────
def test_full_device_order_admin_and_label_override():
    controls = build_controls({
        "setpoint": {"min": 40, "max": 60, "unit": "%", "label": "Humidity target"},
        "ranged": {"min": 1, "max": 3},
        "indicator": {},
    })
    assert [c["kind"] for c in controls] == ["override", "setpoint", "ranged", "indicator"]
    assert all(c["admin"] is True for c in controls)                 # every write is admin-gated
    assert _by_kind(controls)["setpoint"]["label"] == "Humidity target"   # cfg label overrides default
