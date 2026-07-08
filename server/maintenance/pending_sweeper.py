#!/usr/bin/env python3
"""pending_sweeper.py — resolve devices on a migration pending-hold (device_meta.pending_until).

A device believed to have migrated/left is set PENDING by device_push (hidden from the GUI + suppressed
from alerts/ntfy immediately). This sweeper, on a timer, decides each pending device's fate:

  • reported FRESH data within the hold   → CLEAR pending — it never really left (a failed migration
                                            self-heals instead of vanishing); it reappears in the GUI.
  • hold expired with NO fresh data        → DROP: mark `retired` (hidden, but HISTORY KEPT per the
                                            data-storage-is-primary directive) + clear pending.
  • hold still active, no fresh data yet    → leave it holding.

Reuses control_store (device_meta) + hot.db (freshness). Pure `sweep()` core is unit-tested.
"""
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from server.control import control_store as store          # noqa: E402

CONTROL = REPO / "instance/db/control.db"
HOT = REPO / "instance/db/hot.db"
FRESH_S = 1800        # reported within 30 min ⇒ "alive" ⇒ cancel the hold


def _iso_epoch(ts: str):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def _latest_age(hot, device_id: str, now: float):
    """Age (s) of the device's most recent data, or None. device_last_seen first, then readings."""
    r = hot.execute("SELECT last_ts FROM device_last_seen WHERE device_id=?", (device_id,)).fetchone()
    ts = r[0] if r and r[0] else None
    if ts is None:
        r = hot.execute("SELECT MAX(ts) FROM readings WHERE device_id=?", (device_id,)).fetchone()
        ts = r[0] if r else None
    e = _iso_epoch(ts) if ts else None
    return (now - e) if e is not None else None


def sweep(cc, hot, now: float | None = None) -> dict:
    """Resolve every pending device. Pure over the two connections + injected clock (unit-testable)."""
    now = time.time() if now is None else now
    kept = cleared = dropped = 0
    for did, m in store.all_device_meta(cc).items():
        pu = m.get("pending_until")
        if not pu:
            continue
        age = _latest_age(hot, did, now)
        if age is not None and age < FRESH_S:                       # alive → cancel the hold
            store.clear_pending(cc, did); cleared += 1
            print(f"  {did}: fresh (age {int(age)}s) → pending cleared (came back)")
            continue
        exp = _iso_epoch(pu)
        if exp is None or now >= exp:                               # window over (or unparseable) → drop
            store.set_device_meta(cc, did, retired=True, pending_until=None); dropped += 1
            print(f"  {did}: hold expired, no fresh data → dropped (retired; history kept)")
        else:
            kept += 1                                               # still holding, still quiet
    return {"kept": kept, "cleared": cleared, "dropped": dropped}


def main():
    cc = sqlite3.connect(str(CONTROL)); store.ensure_schema(cc)
    hot = sqlite3.connect(f"file:{HOT}?mode=ro", uri=True)
    try:
        print(f"pending-sweep: {sweep(cc, hot)}")
    finally:
        cc.close(); hot.close()


if __name__ == "__main__":
    sys.exit(main())
