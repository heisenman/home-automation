"""Drift-guard for the canonical area taxonomy (ADR-0026 Phase 1).

Single source of truth = ``instance/areas.yaml``. Every ``area:`` referenced anywhere in
the instance configs (devices.yaml, control.yaml, the ``*-devices.yaml`` side-registries)
must resolve to an id declared there. A row that names an area the registry doesn't know
is *drift*, and it fails HERE rather than silently fragmenting the room set the way the
free-form strings did (``office`` / ``infra`` vs ``c_office`` — ADR-0026's own premise).

Runbook: ``docs/design/area-migration-runbook.md``.

Privacy rule: the real ``instance/areas.yaml`` (real room names + floor plan) is gitignored
and delivered out-of-band — it never enters this public repo. Until it lands, the cross-config
drift assertion self-skips; the scanner and the committed generic example are guarded now, so
the moment ``areas.yaml`` exists the guard is live and goes RED on the known drifters
(``office``, ``infra``) = the Phase-2 reconcile punch list.

Scope note: Phase 1 guards the config surface (that's where the drift lives). Extending the
scan to code-level area enums is a follow-on, mirroring how test_agents_nav scoped its passes.
"""
from pathlib import Path

import yaml

from tests._harness import run_module

ROOT = Path(__file__).resolve().parents[1]
AREAS_YAML = ROOT / "instance" / "areas.yaml"
EXAMPLE_YAML = ROOT / "config-examples" / "areas.example.yaml"

# Per the ADR-0026 schema, every area entry carries these.
REQUIRED_KEYS = {"name", "level", "zone", "type"}


def _config_files():
    """Instance configs whose ``area:`` references must resolve. Globbed so a new
    ``*-devices.yaml`` side-registry is covered automatically (no test edit needed)."""
    inst = ROOT / "instance"
    files = [inst / "devices.yaml", inst / "control.yaml"]
    files += sorted(inst.glob("*-devices.yaml"))
    return [f for f in files if f.exists()]


def _iter_area_refs(node):
    """Yield every string value under an ``area`` key, at any nesting depth."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "area" and isinstance(v, str):
                yield v
            else:
                yield from _iter_area_refs(v)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_area_refs(item)


def _area_refs(path):
    return set(_iter_area_refs(yaml.safe_load(path.read_text()) or {}))


def _declared_ids(path):
    """Area ids declared in an areas.yaml-shaped file (top-level ``areas:`` map)."""
    doc = yaml.safe_load(path.read_text()) or {}
    return set((doc.get("areas") or {}).keys())


def test_area_scanner_catches_nested_ref():
    """The scanner isn't a no-op: it finds ``area:`` at depth in mixed list/dict nesting."""
    sample = {"area": "top", "devices": [{"area": "deep_room"}, {"meta": {"area": "nested"}}]}
    assert set(_iter_area_refs(sample)) == {"top", "deep_room", "nested"}, \
        "area scanner missed a reference — the drift-guard would be a no-op"


def test_areas_example_wellformed():
    """The committed generic example parses and every entry carries the schema keys, so the
    documented shape can't drift away from ADR-0026 (and remains a truthful template)."""
    assert EXAMPLE_YAML.exists(), f"missing {EXAMPLE_YAML.relative_to(ROOT)}"
    areas = (yaml.safe_load(EXAMPLE_YAML.read_text()) or {}).get("areas")
    assert isinstance(areas, dict) and areas, "areas.example.yaml has no `areas:` map"
    bad = {aid: sorted(REQUIRED_KEYS - set(spec or {}))
           for aid, spec in areas.items() if REQUIRED_KEYS - set(spec or {})}
    assert not bad, f"areas.example.yaml entries missing required keys {sorted(REQUIRED_KEYS)}: {bad}"


def test_no_area_drift():
    """Every ``area:`` in every instance config resolves to an ``instance/areas.yaml`` id.
    Intended to go RED on the known drifters (``office``, ``infra``) until Phase 2 reconciles
    them; self-skips until the gitignored, out-of-band areas.yaml has landed."""
    if not AREAS_YAML.exists():
        print(f"  SKIP test_no_area_drift: {AREAS_YAML.relative_to(ROOT)} not present yet "
              "(awaiting out-of-band delivery per the privacy rule); guard activates on arrival.")
        return
    valid = _declared_ids(AREAS_YAML)
    assert valid, f"{AREAS_YAML.relative_to(ROOT)} declares no areas"
    drift = {}
    for f in _config_files():
        unknown = sorted(a for a in _area_refs(f) if a not in valid)
        if unknown:
            drift[str(f.relative_to(ROOT))] = unknown
    assert not drift, (
        "area references not declared in instance/areas.yaml (ADR-0026 drift — the Phase-2 "
        "reconcile punch list):\n  " + "\n  ".join(f"{f}: {a}" for f, a in drift.items()))


if __name__ == "__main__":
    run_module(globals())
