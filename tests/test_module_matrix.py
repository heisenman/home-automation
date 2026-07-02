"""Drift-guard for the device x module matrix (edge/MATRIX.md, ADR-0020).

Mirrors test_viewmodel's role for the UI catalog: the generated table MUST equal what the
firmware builds actually link, so a CMakeLists change that isn't reflected in the doc fails here.
Regenerate with: python3 tools/gen_module_matrix.py --write
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import gen_module_matrix as G  # noqa: E402
from tests._harness import run_module  # noqa: E402


def test_matrix_in_sync_with_builds():
    """The committed edge/MATRIX.md matches the table generated from every build's CMakeLists."""
    actual = G.MATRIX.read_text()
    expected = G.expected_doc()
    assert actual == expected, "edge/MATRIX.md is stale — run tools/gen_module_matrix.py --write"


def test_all_builds_parse_nonempty():
    for label, rel in G.BUILDS:
        srcs, reqs = G.parse_cmake(ROOT / rel)
        assert srcs, f"{label}: no SRCS parsed"
        assert reqs, f"{label}: no REQUIRES parsed"


def test_every_module_linked_by_some_build():
    matrix = G.build_matrix()
    for mod, row in matrix.items():
        assert any(v != "—" for v in row.values()), f"{mod} is linked by no build (dead catalog row)"


def test_shared_components_exist_and_are_used():
    """Any module claiming a shared component must have that component dir AND be linked shared somewhere."""
    matrix = G.build_matrix()
    for mod, spec in G.MODULES:
        comp = spec["component"]
        if not comp:
            continue
        assert (ROOT / "firmware" / "components" / comp).is_dir(), f"missing firmware/components/{comp}"
        assert "shared" in matrix[mod].values(), f"{mod} names component {comp} but no build links it shared"


def test_ble_core_fully_migrated():
    """ADR-0020 Stage 2 complete: every build (panel + all edge nodes) links the shared BLE core,
    and no build forks switchbot_decode/ble_scan anymore. Guards against a fork copy sneaking back in."""
    matrix = G.build_matrix()
    for dev in ("d1001-panel", "esp32c6", "esp32c3", "esp32s3-eth"):
        assert matrix["switchbot_decode"][dev] == "shared", f"{dev} not on shared switchbot_decode"
        assert matrix["ble_scan"][dev] == "shared", f"{dev} not on shared ble_scan"
    for mod in ("switchbot_decode", "ble_scan"):
        assert "fork" not in matrix[mod].values(), f"{mod} still forked somewhere"


def test_drift_is_detected():
    """A stale doc must not compare equal to the generated expectation."""
    mangled = G.MATRIX.read_text().replace("| `ble_scan` | shared", "| `ble_scan` | fork", 1)
    assert mangled != G.expected_doc()


if __name__ == "__main__":
    run_module(globals())
