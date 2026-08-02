"""Every systemd unit's Python entrypoint must be able to import what it imports.

Running `python3 server/storage/compactor.py` puts **server/storage/** on `sys.path[0]`, NOT the repo
root — so a top-level `import server.…` raises ModuleNotFoundError even though the same file imports fine
under pytest (which puts the repo root on the path) and under `python3 -m`. The two ways to be safe are a
`sys.path` bootstrap in the file, or `-m` in the unit.

This is a LANDMINE class, not a one-off: the file works everywhere a developer tries it, and breaks only
under systemd. `server/storage/compactor.py` had no bootstrap and no `server.` import for years; `c073de8`
added `from server.storage import latest_cache` and the daily compactor began failing on BOTH boxes at
02:00 the next night — after its DELETE had committed, because the import was deferred. Nothing else in
the repo noticed. This test is what should have noticed.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UNITS = REPO / "systemd"


def _execstart(unit_text: str) -> str | None:
    m = re.search(r'^ExecStart=(.*?)(?=\n[A-Z][A-Za-z]*=|\n\[|\Z)', unit_text, re.M | re.S)
    return re.sub(r'\\\s*\n\s*', ' ', m.group(1)).strip() if m else None


def _script_path_entrypoints():
    """(unit, script) for units that run a repo .py BY PATH — i.e. without `-m`."""
    for unit in sorted(UNITS.glob("*.service")):
        cmd = _execstart(unit.read_text())
        if not cmd or "python" not in cmd or re.search(r'\s-m\s', cmd):
            continue
        script = next((w for w in cmd.split() if w.endswith(".py")), None)
        if script:
            yield unit.name, script


def test_script_path_entrypoints_can_import_the_repo():
    offenders = []
    for unit, script in _script_path_entrypoints():
        p = REPO / script
        if not p.exists():
            offenders.append(f"{unit}: ExecStart references missing {script}")
            continue
        src = p.read_text()
        imports_repo = re.search(r'^\s*(?:from|import)\s+server[\s.]', src, re.M)
        has_bootstrap = "sys.path.insert" in src or "sys.path.append" in src
        if imports_repo and not has_bootstrap:
            offenders.append(
                f"{unit}: runs {script} by path, and it imports `server.…` with no sys.path bootstrap — "
                f"this WILL raise ModuleNotFoundError under systemd while passing every local test")
    assert not offenders, "\n".join(offenders)


def test_compactor_specifically_is_covered():
    """Regression pin for 2026-08-02. Kept explicit so the general test above can never quietly stop
    covering the case that motivated it (e.g. if the unit were reworded)."""
    src = (REPO / "server/storage/compactor.py").read_text()
    assert "sys.path.insert" in src
    # and the latest_cache import is at module scope, so a future breakage fails BEFORE the DELETE
    body = src.split("def compact", 1)[0]
    assert "from server.storage import latest_cache" in body
