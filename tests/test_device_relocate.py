"""server/maintenance/device_relocate.py — the codified area-relocation procedure (ADR-0026 Phase 2).

Covers the pure store operations: the sqlite area update (by-area and by-device, idempotency, dry-run,
missing-column safety) and the registry line editor (both registry shapes, quote/indent/comment
preservation). The SSH peer step and retained-MQTT clear are thin wrappers verified on a real run.
"""
import sqlite3

from server.maintenance import device_relocate as R
from tests._harness import raises


def _hot(tmp_path, readings, last_seen):
    """readings: [(ts, device_id, area)]  ·  last_seen: [(device_id, area)]"""
    p = tmp_path / "hot.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE readings(ts TEXT, device_id TEXT, area TEXT, metric TEXT, value REAL)")
    c.execute("CREATE TABLE device_last_seen(device_id TEXT, area TEXT, last_ts TEXT)")
    c.execute("CREATE TABLE summaries(device_id TEXT, d TEXT)")          # no area column -> must be skipped
    c.executemany("INSERT INTO readings VALUES(?,?,?,'t',1.0)", readings)
    c.executemany("INSERT INTO device_last_seen VALUES(?,?,'t')", last_seen)
    c.commit(); c.close()
    return str(p)


# ── sqlite: area move / device override / idempotency / dry-run / safety ──────────────────────────────
def test_apply_sqlite_area_move_and_idempotent(tmp_path):
    hot = _hot(tmp_path,
               [("t1", "a", "office"), ("t2", "a", "office"), ("t3", "b", "kitchen")],
               [("a", "office"), ("b", "kitchen")])
    out = R.apply_sqlite_area(hot, old_area="office", new_area="h_office", dry_run=False)
    assert out == {"readings": 2, "device_last_seen": 1}
    assert R.count_stale_area(hot, old_area="office") == 0
    # kitchen rows untouched; re-run is a no-op (rows already at h_office are excluded)
    again = R.apply_sqlite_area(hot, old_area="office", new_area="h_office", dry_run=False)
    assert sum(again.values()) == 0
    assert R.count_stale_area(hot, old_area="kitchen") == 2


def test_apply_sqlite_device_override(tmp_path):
    hot = _hot(tmp_path,
               [("t1", "host_210", "h_office"), ("t2", "other", "h_office")],
               [("host_210", "h_office"), ("other", "h_office")])
    out = R.apply_sqlite_area(hot, device_id="host_210", new_area="mech_closet", dry_run=False)
    assert out["readings"] == 1                                  # only host_210's row
    assert R.count_stale_area(hot, device_id="host_210", new_area="mech_closet") == 0
    assert R.count_stale_area(hot, old_area="h_office") == 2     # 'other' still there (readings + last_seen)
    again = R.apply_sqlite_area(hot, device_id="host_210", new_area="mech_closet", dry_run=False)
    assert sum(again.values()) == 0                             # idempotent


def test_forward_only_leaves_readings_history(tmp_path):
    # forward-only move = restrict the update to device_last_seen; readings history stays under the old room.
    hot = _hot(tmp_path,
               [("t1", "host_210", "h_office"), ("t2", "host_210", "h_office"), ("t3", "other", "c_bed")],
               [("host_210", "h_office"), ("other", "c_bed")])
    out = R.apply_sqlite_area(hot, device_id="host_210", new_area="mech_closet", dry_run=False,
                              tables=["device_last_seen"])
    assert out == {"device_last_seen": 1}                                # pointer moved, readings untouched
    # verify scoped to what a forward-only move rewrote = clean; the full-table check still sees stale readings
    assert R.count_stale_area(hot, device_id="host_210", new_area="mech_closet",
                              tables=["device_last_seen"]) == 0
    assert R.count_stale_area(hot, device_id="host_210", new_area="mech_closet") == 2   # 2 readings still old


def test_apply_sqlite_dry_run_writes_nothing(tmp_path):
    hot = _hot(tmp_path, [("t1", "a", "office")], [("a", "office")])
    out = R.apply_sqlite_area(hot, old_area="office", new_area="h_office", dry_run=True)
    assert out["readings"] == 1 and out["device_last_seen"] == 1
    assert R.count_stale_area(hot, old_area="office") == 2      # unchanged


def test_apply_sqlite_requires_exactly_one_selector(tmp_path):
    hot = _hot(tmp_path, [("t1", "a", "office")], [("a", "office")])
    with raises(ValueError):
        R.apply_sqlite_area(hot, old_area="office", device_id="a", new_area="x", dry_run=True)
    with raises(ValueError):
        R.apply_sqlite_area(hot, new_area="x", dry_run=True)


def test_devices_in_area(tmp_path):
    hot = _hot(tmp_path, [], [("a", "office"), ("b", "office"), ("c", "kitchen")])
    assert sorted(R.devices_in_area(hot, "office")) == ["a", "b"]


# ── registry line editor: area move (quote/indent/comment preserved across all 4 shapes) ─────────────
DEVICES_YAML = (
    'devices:\n'
    '  "B0:E9:FE:54:AB:A2":\n'
    '    device_id: "meter_pro_master_bed"\n'
    '    area: "master_bedroom"\n'
    '    capabilities: [temperature]\n'
    '  "B0:E9:FE:54:AE:6B":\n'
    '    device_id: "meter_pro_c_office"\n'
    '    area: "c_office"\n'
)
CONTROL_YAML = (
    'devices:\n'
    '  levoit_office:\n'
    '    node: server\n'
    '    area: office\n'
    '  host_210:\n'
    '    node: server\n'
    '    area: office                 # panel physically in the office\n'
)
TASMOTA_YAML = (
    'plug_g11:\n'
    '  device_id: plug_g11\n'
    '  area: infra            # the G11 box wall-power meter\n'
    '  device_type: energy_meter\n'
)


def test_area_move_preserves_quotes_and_indent():
    out, n = R.relocate_area_in_text(DEVICES_YAML, "master_bedroom", "h_office")
    assert n == 1
    assert '    area: "h_office"\n' in out          # quotes + 4-space indent kept
    assert '    area: "c_office"\n' in out          # other area untouched


def test_area_move_preserves_trailing_comment():
    out, n = R.relocate_area_in_text(CONTROL_YAML, "office", "h_office")
    assert n == 2                                    # both office entries
    assert 'area: h_office                 # panel physically in the office\n' in out


def test_area_move_two_space_indent_with_comment():
    out, n = R.relocate_area_in_text(TASMOTA_YAML, "infra", "mech_closet")
    assert n == 1
    assert '  area: mech_closet            # the G11 box wall-power meter\n' in out


def test_area_move_no_match_is_noop():
    out, n = R.relocate_area_in_text(CONTROL_YAML, "nowhere", "x")
    assert n == 0 and out == CONTROL_YAML


# ── registry line editor: device override (both entry shapes) ────────────────────────────────────────
def test_device_override_style_a_keyed_by_device_id():
    """control.yaml: the entry key IS the device_id."""
    out, n = R.relocate_device_in_text(CONTROL_YAML, "host_210", "mech_closet")
    assert n == 1
    # only host_210 moved; levoit_office (also 'office') untouched
    assert 'host_210:\n    node: server\n    area: mech_closet' in out
    assert '  levoit_office:\n    node: server\n    area: office\n' in out


def test_device_override_style_b_device_id_field():
    """devices.yaml / *-devices.yaml: the entry carries a device_id: field; area is its sibling."""
    out, n = R.relocate_device_in_text(DEVICES_YAML, "meter_pro_master_bed", "h_bed")
    assert n == 1
    assert '    area: "h_bed"\n' in out
    assert '    area: "c_office"\n' in out          # sibling entry untouched


def test_device_override_idempotent_and_missing():
    once, n1 = R.relocate_device_in_text(CONTROL_YAML, "host_210", "mech_closet")
    twice, n2 = R.relocate_device_in_text(once, "host_210", "mech_closet")
    assert n1 == 1 and n2 == 0 and twice == once     # second run is a no-op
    _, n3 = R.relocate_device_in_text(CONTROL_YAML, "no_such_device", "x")
    assert n3 == 0


if __name__ == "__main__":
    from tests._harness import run_module
    run_module(globals())
