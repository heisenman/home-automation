"""End-to-end device rename + relocate from a worksheet — hands-off, no by-hand steps (ADR-0026 Phase 3).

Reads a worksheet ``{devices: {old_id: {new_id, new_area}}}`` (default ``instance/device-rename.yaml``,
gitignored) and drives the WHOLE process in code so it is repeatable without an LLM or a human in the loop:

  1. APPLY   — per device: identity rename (device_migrate.rename_everywhere: data + registry + control.db +
               secret + peer) and/or location move (device_relocate.relocate: area across data + registry +
               retained + peer).
  2. RESTART — bounce every ingest service that holds a registry in memory, or it keeps stamping the old
               id/area and re-regresses device_last_seen (the lesson from the Phase-2 live run).
  3. SWEEP   — re-apply (idempotent) to migrate rows ingested under the old id/area during the apply→restart
               window (the migrate-while-live race).
  4. RETAINED— clear the OLD ``home/<old_area>/<old_id>/state`` topics. Old paths are derived from the
               worksheet plan captured BEFORE mutation (device_last_seen has already moved, so the tools
               can't rederive them) — this is the fix for the straggler retained messages.
  5. VERIFY  — tests.test_areas green + no stale old id/area rows remain locally.

``--dry-run`` does step 1 only (writes nothing) and prints the plan. Idempotent; a second real run is a no-op.
Backups: each underlying tool backs up hot.db + rungs + control.db + every registry file + secrets first.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import yaml

from server.maintenance import device_migrate as M
from server.maintenance import device_relocate as R

REPO_ROOT = M.REPO_ROOT

# Every service that reads a registry and stamps area/device_id onto readings (Phase-2 lesson). Bouncing
# only the mapper is NOT enough — the BLE writer/scanner + the bridges + controller each cache their registry.
INGEST_SERVICES = ["ha-scanner", "ha-writer", "ha-edge-mapper", "ha-edge-history",
                   "ha-tasmota-bridge", "ha-levoit-bridge", "ha-controller",
                   "ha-relay-coordinator", "ha-api", "ha-api-tls"]

SWEEP_MAX_PASSES = 4      # bound the tail-race sweep loop
SWEEP_SETTLE_S = 6        # let in-flight readings land between sweeps (BLE meters report ~60s)

# Maintenance-inhibit flag: while fresh, the keepalived healthcheck reports FIT so this op's fleet restart
# (which bounces ha-api) can't trip a spurious failover — the op must not flap the VIP it runs under
# (failover-primitives lesson). Bounded by MAINT_FIT_MAX_AGE in failover/healthcheck.sh; cleared in finally.
FIT_FLAG = REPO_ROOT / "instance" / ".maintenance-fit"


def _set_maintenance_fit() -> None:
    try:
        FIT_FLAG.write_text("maintenance")
    except OSError:                       # non-clustered box / read-only instance — nothing reads the flag
        pass


def _clear_maintenance_fit() -> None:
    try:
        FIT_FLAG.unlink()
    except OSError:                       # includes not-present
        pass


def registry_area_map(inst: Path | None = None) -> dict:
    """device_id -> current area, read from every registry (the pre-migration source of truth).

    A device has ONE home registry that carries its area; some registries (e.g. panel-devices.yaml) map a
    node to a device_id but hold no area. Never let such an entry overwrite a real area with None."""
    out: dict[str, str] = {}
    inst = inst or (REPO_ROOT / "instance")

    def _put(did, area):
        if did and area is not None:
            out[did] = area

    dev = yaml.safe_load((inst / "devices.yaml").read_text()) or {}
    for e in (dev.get("devices") or {}).values():
        if isinstance(e, dict):
            _put(e.get("device_id"), e.get("area"))
    ctl = yaml.safe_load((inst / "control.yaml").read_text()) or {}
    for did, e in (ctl.get("devices") or {}).items():
        if isinstance(e, dict):
            _put(did, e.get("area"))
    for p in sorted(inst.glob("*-devices.yaml")):
        y = yaml.safe_load(p.read_text()) or {}
        for e in y.values():
            if isinstance(e, dict):
                _put(e.get("device_id"), e.get("area"))
    return out


def _last_seen_area(device_id: str, hot_db: Path) -> str | None:
    """Area of a device from device_last_seen — the fallback for a straggler whose registry entry has
    already been renamed away (keeps a re-run idempotent + retained paths derivable)."""
    try:
        conn = sqlite3.connect(hot_db, timeout=30)
        try:
            r = conn.execute("SELECT area FROM device_last_seen WHERE device_id=?", (device_id,)).fetchone()
            return r[0] if r else None
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def load_plan(worksheet: str = "instance/device-rename.yaml") -> list[dict]:
    """Build the per-device plan; skip rows that are no-ops. Each: old_id/new_id/old_area/new_area +
    id_change/area_change flags."""
    p = (REPO_ROOT / worksheet) if not Path(worksheet).is_absolute() else Path(worksheet)
    doc = yaml.safe_load(p.read_text()) or {}
    areas = registry_area_map()
    hot_db = REPO_ROOT / "instance" / "db" / "hot.db"
    plan = []
    for old_id, spec in (doc.get("devices") or {}).items():
        spec = spec or {}
        new_id = spec.get("new_id") or old_id
        # registry is authoritative; fall back to device_last_seen for a straggler mid-recovery
        old_area = areas.get(old_id) or _last_seen_area(old_id, hot_db)
        new_area = spec.get("new_area") or old_area
        id_change, area_change = (new_id != old_id), (new_area != old_area)
        if id_change or area_change:
            plan.append({"old_id": old_id, "new_id": new_id, "old_area": old_area,
                         "new_area": new_area, "id_change": id_change, "area_change": area_change})
    return plan


def single_device_plan(device_id: str, *, new_id: str | None = None, new_area: str | None = None,
                       restamp: bool = True) -> list[dict]:
    """One-device plan for the UI-driven admin path (the single-Entity analogue of ``load_plan``). An
    unspecified target keeps the current value; ``restamp`` carries the relocate mode (True = history-safe,
    False = forward-only). Areas resolve from the registries (authoritative), falling back to
    ``device_last_seen``. Returns ``[]`` for a no-op. Shape matches ``load_plan`` + a ``restamp`` field that
    ``apply_one``/``stale_counts`` read (worksheet plans omit it → default True)."""
    areas = registry_area_map()
    hot_db = REPO_ROOT / "instance" / "db" / "hot.db"
    old_area = areas.get(device_id) or _last_seen_area(device_id, hot_db)
    nid = new_id or device_id
    narea = new_area or old_area
    id_change, area_change = (nid != device_id), (narea != old_area)
    if not (id_change or area_change):
        return []
    return [{"old_id": device_id, "new_id": nid, "old_area": old_area, "new_area": narea,
             "id_change": id_change, "area_change": area_change, "restamp": bool(restamp)}]


def validate_plan(plan: list[dict], valid_areas: set | None = None) -> list[str]:
    """Fail fast on unresolvable target areas + id collisions (drift-guard the worksheet before mutating)."""
    valid = valid_areas if valid_areas is not None else \
        set((yaml.safe_load((REPO_ROOT / "instance" / "areas.yaml").read_text()) or {}).get("areas") or {})
    errs = []
    seen_new = {}
    for s in plan:
        if s["new_area"] not in valid:
            errs.append(f"{s['old_id']}: new_area '{s['new_area']}' is not a canonical areas.yaml id")
        seen_new.setdefault(s["new_id"], []).append(s["old_id"])
    for nid, srcs in seen_new.items():
        if len(srcs) > 1:
            errs.append(f"new_id '{nid}' claimed by multiple devices: {srcs} (would collide)")
    return errs


def apply_one(step: dict, *, dry_run: bool, do_peer: bool = True) -> dict:
    """Rename (if id changes) then relocate (if area changes) one device. Relocate targets the NEW id."""
    rep = {"old_id": step["old_id"], "new_id": step["new_id"], "new_area": step["new_area"]}
    cur_id = step["old_id"]
    if step["id_change"]:
        rep["rename"] = M.rename_everywhere(step["old_id"], step["new_id"], do_peer=do_peer, dry_run=dry_run)
        if not dry_run:
            cur_id = step["new_id"]
    if step["area_change"]:
        rep["relocate"] = R.relocate("device", cur_id, step["new_area"], do_peer=do_peer, dry_run=dry_run,
                                     restamp_history=step.get("restamp", True))
    return rep


RECONCILE_SERVICE = "ha-reconcile-history"   # bidirectional peer merge on UNIQUE(device_id,ts,metric)


def _systemctl(action: str, *services: str) -> str:
    r = subprocess.run(["sudo", "systemctl", action, *services], capture_output=True, text=True, timeout=120)
    return "ok" if r.returncode == 0 else f"ERROR: {(r.stderr or r.stdout).strip()[:300]}"


def _present_units(services: list[str]) -> list[str]:
    """Only the units that actually exist on THIS box — the peer (.245) has a different service set, so
    restarting the dictator's full list there errors on absent units (fixed in code, not by hand)."""
    return [s for s in services
            if subprocess.run(["systemctl", "cat", s], capture_output=True, text=True).returncode == 0]


def restart_ingest(dry_run: bool) -> str:
    if dry_run:
        return "skipped (dry-run)"
    present = _present_units(INGEST_SERVICES)
    absent = [s for s in INGEST_SERVICES if s not in present]
    err = _systemctl("restart", *present) if present else "ok"
    if err != "ok":
        return err
    bad = [s for s in present
           if subprocess.run(["systemctl", "is-active", s], capture_output=True, text=True).stdout.strip()
           != "active"]
    note = f" (skipped absent: {absent})" if absent else ""
    return ("all active" if not bad else f"NOT active: {bad}") + note


def clear_old_retained(plan: list[dict], *, broker: str = "localhost", dry_run: bool) -> list[str]:
    """Clear the OLD retained state topics (old_area/old_id derived from the plan, captured pre-migration)."""
    topics = [f"home/{s['old_area']}/{s['old_id']}/state" for s in plan if s["old_area"]]
    return M.clear_retained(broker, 1883, topics, dry_run=dry_run)


def stale_counts(plan: list[dict]) -> dict:
    """Rows still carrying an old id, or the new id with a stale area — the straggler set to sweep to 0."""
    hot = str(REPO_ROOT / "instance" / "db" / "hot.db")
    stale = {}
    for s in plan:
        if s["id_change"]:
            n = M.count_sqlite(hot, M.HOT_TABLES, s["old_id"])
            if n:
                stale[f"old_id:{s['old_id']}"] = n
        if s["area_change"]:
            tables = R.AREA_TABLES if s.get("restamp", True) else ["device_last_seen"]
            n = R.count_stale_area(hot, device_id=s["new_id"], new_area=s["new_area"], tables=tables)
            if n:
                stale[f"stale_area:{s['new_id']}"] = n
    return stale


def verify(plan: list[dict]) -> dict:
    """Op success = **no stale** (old-id / old-area) rows remain for the moved entity. ``test_areas`` is a
    WHOLE-SYSTEM area-hygiene check kept as an ADVISORY signal only — it must NOT flip a clean op to failed:
    a relocate can't introduce area drift (``validate_plan`` gates the target to a canonical ``areas.yaml``
    area) and a rename doesn't touch areas, so a ``test_areas`` failure is always PRE-EXISTING/unrelated
    drift on the box (surfaced by the ha-2 cutover: a device parked in a non-canonical ``standby`` area made
    every op report "failed"). ``clean`` therefore reflects the op, not the box's pre-existing hygiene."""
    stale = stale_counts(plan)
    t = subprocess.run([sys.executable, "-m", "tests.test_areas"], cwd=str(REPO_ROOT),
                       capture_output=True, text=True)
    return {"test_areas_green": t.returncode == 0, "stale": stale, "clean": not stale}


def run_plan(plan: list[dict], *, dry_run: bool = False, do_restart: bool = True,
             do_peer: bool = True, broker: str = "localhost") -> dict:
    """Orchestrate an already-built plan — the shared core of the worksheet CLI (``run``) and the
    single-device admin path (``single_device_plan``). Validates target areas + id collisions first, then:
    1. apply  2. restart ingest fleet  3. bounded idempotent sweep  4. clear old retained  5. verify —
    with peer reconcile paused across the mutation (try/finally). ``dry_run`` does step 1 only (no writes).
    ``do_restart=False`` = data-only (skip restart + sweep, e.g. ON the peer)."""
    errs = validate_plan(plan)
    if errs:
        return {"error": "invalid plan", "problems": errs}
    report: dict = {"dry_run": dry_run, "planned": len(plan), "steps": []}
    if not plan:
        report["clean"] = True
        return report
    if dry_run:
        for s in plan:
            report["steps"].append(apply_one(s, dry_run=True, do_peer=do_peer))
        report["plan"] = [{k: s[k] for k in ("old_id", "new_id", "old_area", "new_area")} for s in plan]
        return report
    # Pause peer reconcile so it can't re-insert an old device_id mid-rename; always resume (try/finally).
    report["reconcile_paused"] = _systemctl("stop", RECONCILE_SERVICE)
    try:
        for s in plan:
            report["steps"].append(apply_one(s, dry_run=False, do_peer=do_peer))
        if do_restart:
            _set_maintenance_fit()        # hold keepalived FIT across the fleet restart (no spurious failover)
            report["restart"] = restart_ingest(False)
            # Sweep until the tail-race settles: an in-flight reading can land under the old id/area just
            # after a sweep, so re-sweep with a short settle until no stragglers remain (bounded).
            report["sweep_passes"] = 0
            for _ in range(SWEEP_MAX_PASSES):
                for s in plan:
                    apply_one(s, dry_run=False, do_peer=do_peer)
                report["sweep_passes"] += 1
                if not stale_counts(plan):
                    break
                time.sleep(SWEEP_SETTLE_S)
        else:
            report["restart"] = "skipped (data-only / --no-restart)"
        report["retained_cleared"] = clear_old_retained(plan, broker=broker, dry_run=False)
        report["verify"] = verify(plan)
        report["clean"] = report["verify"]["clean"]
    finally:
        _clear_maintenance_fit()          # re-arm the healthcheck the instant the op ends (or aborts)
        report["reconcile_resumed"] = _systemctl("start", RECONCILE_SERVICE)
    return report


def run(worksheet: str = "instance/device-rename.yaml", *, dry_run: bool = False,
        do_restart: bool = True, do_peer: bool = True, broker: str = "localhost") -> dict:
    return run_plan(load_plan(worksheet), dry_run=dry_run, do_restart=do_restart,
                    do_peer=do_peer, broker=broker)


def _main() -> int:
    import argparse
    import json
    p = argparse.ArgumentParser(description="Apply a device rename+relocate worksheet end-to-end")
    p.add_argument("worksheet", nargs="?", default="instance/device-rename.yaml")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-restart", action="store_true", help="skip the service restart + sweep (data only)")
    p.add_argument("--no-peer", action="store_true", help="skip the .245 peer step (use ON the peer itself)")
    p.add_argument("--broker", default="localhost")
    a = p.parse_args()
    rep = run(a.worksheet, dry_run=a.dry_run, do_restart=not a.no_restart, do_peer=not a.no_peer,
              broker=a.broker)
    print(json.dumps(rep, indent=2))
    return 0 if (rep.get("dry_run") or rep.get("clean")) else 2


if __name__ == "__main__":
    raise SystemExit(_main())
