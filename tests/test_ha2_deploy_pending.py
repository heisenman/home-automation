"""Tripwire: keep docs/airgap/HA2-DEPLOY-PENDING.md in sync with the `HA2-DEPLOY-DRIFT:<id>` code markers.

The air-gap means git-committed != deployed-on-ha-2 (ADR-0034 / [[airgap-checkout-drift]]). Each committed-
but-undeployed change plants a `HA2-DEPLOY-DRIFT:<id>` marker at its site and a row in the ledger. This test
asserts the two never drift apart:

  • a marker with NO ledger row  -> fail (document the pending deploy in HA2-DEPLOY-PENDING.md)
  • a ledger row with NO marker   -> fail (deployed it? remove the row. else restore the marker.)

So an item can only leave the list by removing BOTH — which is exactly what "I actually deployed it to ha-2"
should look like. A dev who prematurely rips out a tripwire, or silently drops a ledger row, goes red.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LEDGER = REPO / "docs" / "airgap" / "HA2-DEPLOY-PENDING.md"
# code trees that may carry a live drift marker (docs/ + tests/ are excluded: the ledger + this file mention
# the token by necessity and must not count as code markers)
SCAN_DIRS = ["server", "tools", "systemd", "failover"]
SCAN_SUFFIXES = {".py", ".sh", ".service", ".timer", ".c", ".h", ".yaml", ".yml"}
MARK = re.compile(r"HA2-DEPLOY-DRIFT:([a-z0-9][a-z0-9\-]*)")


def _ledger_ids() -> set[str]:
    return set(MARK.findall(LEDGER.read_text())) if LEDGER.exists() else set()


def _code_markers() -> dict[str, list[str]]:
    hits: dict[str, list[str]] = {}
    for d in SCAN_DIRS:
        base = REPO / d
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.is_file() and p.suffix in SCAN_SUFFIXES:
                for mid in MARK.findall(p.read_text(errors="ignore")):
                    hits.setdefault(mid, []).append(str(p.relative_to(REPO)))
    return hits


def test_ledger_exists():
    assert LEDGER.exists(), f"deploy-pending ledger missing: {LEDGER.relative_to(REPO)}"


def test_every_code_marker_has_a_ledger_row():
    code, ledger = _code_markers(), _ledger_ids()
    missing = {k: v for k, v in code.items() if k not in ledger}
    assert not missing, ("HA2-DEPLOY-DRIFT markers with no ledger row — add them to "
                         f"docs/airgap/HA2-DEPLOY-PENDING.md: {missing}")


def test_every_ledger_row_has_a_code_marker():
    code, ledger = _code_markers(), _ledger_ids()
    orphan = ledger - set(code)
    assert not orphan, ("ledger rows with no code marker — if deployed to ha-2, delete the row; "
                        f"otherwise restore the marker: {sorted(orphan)}")
