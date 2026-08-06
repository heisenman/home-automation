"""PWA chart hover readout — runs the JS assertions in tests/web/chart_hover.mjs under node.

The interesting logic in AdaptiveChart's hover readout is arithmetic (nearest-sample binary search,
the px<->time round trip through a stretched viewBox, the staleness gate), so it's testable without a
DOM. Skips if node isn't installed — the server venv doesn't depend on it.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "tests" / "web" / "chart_hover.mjs"


def test_chart_hover_math():
    node = shutil.which("node")
    if not node:
        print("    (skip: node not installed)")
        return
    r = subprocess.run([node, str(SCRIPT)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"chart_hover.mjs failed:\n{r.stdout}\n{r.stderr}"


def test_app_js_parses_as_a_module():
    """app.js is served unbuilt, so a syntax error ships straight to the browser. `node --check` on a
    .mjs copy is the cheapest gate (plain --check false-passes nested htm templates)."""
    node = shutil.which("node")
    if not node:
        print("    (skip: node not installed)")
        return
    import tempfile
    for name in ("app.js", "sw.js", "push.js"):
        src = REPO / "server" / "web" / name
        with tempfile.TemporaryDirectory() as d:
            copy = Path(d) / (src.stem + ".mjs")
            copy.write_text(src.read_text())
            r = subprocess.run([node, "--check", str(copy)], capture_output=True, text=True, timeout=60)
            assert r.returncode == 0, f"{name} is not a valid ES module:\n{r.stderr}"


if __name__ == "__main__":
    from tests._harness import run_module
    run_module(globals())
