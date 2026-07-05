"""device_migrate identity extensions (ADR-0026 Phase 3): registry device_id + control_secrets rewriting.

The data-store rename is covered by test_device_migrate; here we cover the NEW surfaces a complete rename
must also touch — the registry `device_id` (as a field value AND as a control.yaml entry key) and the
control_secrets key — via the pure line editors.
"""
import sqlite3

from server.maintenance import device_migrate as M


def test_apply_sqlite_rename_merges_on_unique_collision(tmp_path):
    """A rename must not crash when the new_id already holds a row (a race-window straggler meeting a
    restarted-ingest row). device_last_seen has a UNIQUE device_id -> old->new merges via OR REPLACE."""
    p = tmp_path / "hot.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE device_last_seen(device_id TEXT PRIMARY KEY, area TEXT)")
    c.execute("INSERT INTO device_last_seen VALUES('old','a')")
    c.execute("INSERT INTO device_last_seen VALUES('new','b')")     # new already present -> would collide
    c.commit(); c.close()
    out = M.apply_sqlite(str(p), ["device_last_seen"], "old", "new", retire=False, dry_run=False)
    assert out["device_last_seen"] == 1                             # no IntegrityError
    c = sqlite3.connect(p)
    assert c.execute("SELECT COUNT(*) FROM device_last_seen WHERE device_id='new'").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM device_last_seen WHERE device_id='old'").fetchone()[0] == 0
    c.close()


def test_rename_key_line_renames_key_preserving_value():
    # control.yaml entry key (no value) and control_secrets entry (key: value)
    assert M._rename_key_line("  levoit_office:", "levoit_office", "purifier_h_office") == "  purifier_h_office:"
    assert M._rename_key_line("  levoit_office: abc123", "levoit_office", "purifier_h_office") \
        == "  purifier_h_office: abc123"
    # trailing comment preserved
    assert M._rename_key_line("  host_210:   # box", "host_210", "rack_210") == "  rack_210:   # box"


def test_rename_key_line_ignores_non_matching_and_values():
    assert M._rename_key_line("    area: office", "office", "h_office") is None       # 'office' is a value, not the key
    assert M._rename_key_line("    device_id: levoit_office", "levoit_office", "x") is None  # field, handled elsewhere
    assert M._rename_key_line("  other_device:", "levoit_office", "x") is None


def test_devid_field_regex_matches_quoted_and_unquoted():
    m = M._DEVID_FIELD_RE.match('    device_id: "meter_h_bed"')
    assert m and m.group("val") == "meter_h_bed"
    m = M._DEVID_FIELD_RE.match("  device_id: levoit_office")
    assert m and m.group("val") == "levoit_office"


def test_apply_registry_id_field_and_key(tmp_path):
    """Rewrite a device_id as a `device_id:` field (devices/side-registry) and as a control.yaml key."""
    devices = tmp_path / "devices.yaml"
    devices.write_text('devices:\n  "B0:E9:FE:54:AB:A2":\n    device_id: "meter_pro_master_bed"\n    area: "h_bed"\n')
    control = tmp_path / "control.yaml"
    control.write_text('devices:\n  levoit_office:\n    node: server\n    area: h_office\n')
    levoit = tmp_path / "levoit-devices.yaml"
    levoit.write_text('levoit-office:\n  device_id: levoit_office\n  area: h_office\n')

    orig = M._registry_paths
    M._registry_paths = lambda: [devices, control, levoit]           # point the tool at the fixtures
    try:
        out1 = M.apply_registry_id("meter_pro_master_bed", "meter_pro_h_bed", dry_run=False)
        out2 = M.apply_registry_id("levoit_office", "purifier_h_office", dry_run=False)
    finally:
        M._registry_paths = orig

    assert out1 == {"devices.yaml": 1}
    assert '    device_id: "meter_pro_h_bed"\n' in devices.read_text()
    # levoit_office is BOTH a control.yaml key and a levoit-devices.yaml field value -> both rewritten
    assert out2 == {"control.yaml": 1, "levoit-devices.yaml": 1}
    assert "  purifier_h_office:\n" in control.read_text()
    assert "  device_id: purifier_h_office\n" in levoit.read_text()


def test_apply_registry_id_dry_run_writes_nothing(tmp_path):
    control = tmp_path / "control.yaml"
    control.write_text('devices:\n  levoit_office:\n    area: h_office\n')
    orig = M._registry_paths
    M._registry_paths = lambda: [control]
    try:
        out = M.apply_registry_id("levoit_office", "purifier_h_office", dry_run=True)
    finally:
        M._registry_paths = orig
    assert out == {"control.yaml": 1}
    assert "  levoit_office:\n" in control.read_text()               # unchanged on disk


def test_rename_secret_line(tmp_path):
    p = tmp_path / "control_secrets.yaml"
    p.write_text("# secrets\ndehumidifier_office: deadbeef\nlevoit_office: cafef00d\n")
    assert M.rename_secret("levoit_office", "purifier_h_office", dry_run=False, path=str(p)) is True
    assert "purifier_h_office: cafef00d\n" in p.read_text()
    assert "dehumidifier_office: deadbeef\n" in p.read_text()        # other secret untouched
    assert M.rename_secret("no_such_device", "x", dry_run=False, path=str(p)) is False


if __name__ == "__main__":
    from tests._harness import run_module
    run_module(globals())
