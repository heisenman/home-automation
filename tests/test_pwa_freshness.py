"""PWA freshness + fetch deadlines — runs the JS assertions in tests/web/freshness.mjs under node.

Guards the 2026-09-13 regression: the dashboard sat on frozen readings behind a green "live" dot, and the
numbers were read as current for a fortnight. Nothing was wrong with the server — the app simply had no way
to tell that it had stopped receiving data, because the only freshness signal it had (`age_s`) travels
INSIDE the payload and freezes along with it.

The decision core (dataAge/isStale/statusDot) and the fetch wrapper are pure, so this needs no DOM — same
approach as tests/test_chart_hover.py. Skips if node isn't installed; the server venv doesn't depend on it.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "tests" / "web" / "freshness.mjs"


def test_pwa_freshness_and_deadlines():
    node = shutil.which("node")
    if not node:
        print("    (skip: node not installed)")
        return
    r = subprocess.run([node, str(SCRIPT)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"freshness.mjs failed:\n{r.stdout}\n{r.stderr}"


def test_every_poll_endpoint_carries_a_deadline():
    """The dashboard poll must never call getJSON bare — a bare call inherits the generous read default,
    which also bounds how fast recovery is noticed (the in-flight guard skips ticks while one is pending).
    """
    src = (REPO / "server" / "web" / "app.js").read_text()
    block = src[src.index("const [disp, sens, alr] = await Promise.all(["):]
    block = block[:block.index("]);")]
    for path in ("/api/v1/displays", "/api/v1/sensors", "/api/v1/alerts"):
        line = next(ln for ln in block.splitlines() if path in ln)
        assert "POLL_TIMEOUT_MS" in line, f"poll of {path} has no explicit deadline: {line.strip()}"


def test_no_bare_fetch_outside_the_wrapper():
    """Every request has to go through fetchJSON, or it has no deadline and can hang forever — the exact
    shape of the original bug. The wrapper's own call is the single legitimate `fetch(`.

    SCOPE: app.js only. push.js still calls fetch() bare in three places (vapid-public-key, subscribe,
    unsubscribe). Same class of bug, much smaller blast radius — the worst case is the notification
    toggle stuck mid-flip, not readings that lie. Sharing the wrapper means a new module in the shell
    precache list, so it is deliberately left for its own change rather than smuggled into this one.
    """
    src = (REPO / "server" / "web" / "app.js").read_text()
    hits = [ln.strip() for ln in src.splitlines()
            if "fetch(" in ln and "fetchJSON(" not in ln and not ln.strip().startswith("//")]
    assert hits == ["const r = await fetch(path, { ...opts, signal: ac.signal });"], \
        "unwrapped fetch() found in app.js:\n" + "\n".join(hits)


if __name__ == "__main__":
    from tests._harness import run_module
    run_module(globals())
