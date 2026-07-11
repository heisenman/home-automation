"""Detached admin-maintenance jobs (UI-driven device rename / relocate).

The mutating orchestration (``apply_rename_worksheet.run_plan``) restarts the ingest fleet — *including*
``ha-api``/``ha-api-tls`` — so it cannot run inside an ha-api request without killing that request, and the
bounded sweep runs ~20-40s (too long to hold a request behind the :443 bridge). So the API launches it
**out-of-process** as a oneshot systemd unit that survives the self-restart, and reports through a durable
JSON file the API polls (ADR/`docs/design/ui-device-admin.md`, option A).

Launch path — **zero new sudo**: ``sudo systemctl start ha-admin-job@<id>.service`` is already covered by
the ``systemctl start ha-*`` NOPASSWD rule; the template unit (``systemd/ha-admin-job@.service``) runs the
``run`` worker below as ``visko`` (no ``NoNewPrivileges`` — the worker needs ``sudo systemctl restart ha-*``).

Job lifecycle (status field): ``running`` → ``done`` (clean) | ``failed`` (verify unclean) | ``error``.
"""
from __future__ import annotations

import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
JOB_DIR = REPO_ROOT / "instance" / "db" / "admin-jobs"
QUEUE_DIR = JOB_DIR / "queue"                        # a marker here triggers ha-admin-job.path -> dispatch
JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")            # our own ids; also the guard on any id used as a path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_path(job_id: str) -> Path:
    return JOB_DIR / f"{job_id}.json"


def _write(job_id: str, data: dict) -> None:
    """Atomic write so a poll never reads a half-written report."""
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _job_path(job_id).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(_job_path(job_id))


def status(job_id: str) -> dict | None:
    """The job record, or None if unknown. A transient JSONDecodeError (mid-write) reads as still-running."""
    if not JOB_ID_RE.match(job_id or ""):
        return None
    p = _job_path(job_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {"job_id": job_id, "status": "running"}


def launch(spec: dict) -> str:
    """Enqueue a maintenance job and return its id — with NO privilege. The API (``ha-api``) runs under a
    ``NoNewPrivileges`` sandbox and therefore CANNOT ``sudo``, so it must not start the unit itself (that was
    the silent-hang bug: the record was written but ``sudo systemctl start`` was blocked by the sandbox).
    Instead it just writes the job record + a queue marker (both under ``instance/``, in the API's
    ReadWritePaths). A system ``ha-admin-job.path`` unit watches the queue and runs ``dispatch`` in an
    un-sandboxed service that CAN restart the fleet. ``spec`` = {op: rename|relocate, device_id, new_id?,
    new_area?, restamp?}."""
    job_id = uuid.uuid4().hex[:12]
    _write(job_id, {"job_id": job_id, "status": "running", "spec": spec, "started": _now()})
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    (QUEUE_DIR / job_id).write_text("")              # presence triggers ha-admin-job.path -> dispatch
    return job_id


def dispatch() -> int:
    """Run by the ha-admin-job.path-triggered service (un-sandboxed → run_plan can ``sudo`` the fleet
    restart; own cgroup → survives the ha-api restart it triggers). Process every queued job to a terminal
    state; remove each marker only AFTER its worker finishes, so a crash leaves the marker for a re-trigger."""
    if not QUEUE_DIR.exists():
        return 0
    rc = 0
    for marker in sorted(QUEUE_DIR.iterdir()):
        if not JOB_ID_RE.match(marker.name):
            try: marker.unlink()                     # ignore junk in the queue dir
            except OSError: pass
            continue
        rc = _worker(marker.name) or rc
        try: marker.unlink()
        except OSError: pass
    return rc


def _worker(job_id: str) -> int:
    """Runs INSIDE the detached unit: build the single-device plan from the recorded spec, run the full
    orchestration, write the final report. Never raises out — always leaves a terminal record."""
    from server.maintenance import apply_rename_worksheet as A
    rec = status(job_id) or {"job_id": job_id, "status": "running"}
    spec = rec.get("spec") or {}
    try:
        op, did = spec.get("op"), spec.get("device_id")
        if not did:
            raise ValueError("spec missing device_id")
        if op == "rename":
            plan = A.single_device_plan(did, new_id=spec.get("new_id"))
        elif op == "relocate":
            plan = A.single_device_plan(did, new_area=spec.get("new_area"),
                                        restamp=spec.get("restamp", True))
        else:
            raise ValueError(f"unknown op {op!r}")
        if not plan:
            rec.update(status="done", report={"planned": 0, "clean": True, "note": "no-op"}, finished=_now())
        else:
            report = A.run_plan(plan, dry_run=False, do_restart=True, do_peer=True)
            clean = bool(report.get("clean")) and "error" not in report
            rec.update(status=("done" if clean else "failed"), report=report, finished=_now())
    except Exception as e:   # any failure: leave a terminal error record for the poller
        rec.update(status="error", error=str(e), finished=_now())
    _write(job_id, rec)
    return 0 if rec["status"] == "done" else 2


def _main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="admin maintenance job worker/status")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("dispatch", help="run all queued jobs (invoked by ha-admin-job.path)")
    r = sub.add_parser("run", help="run one recorded job by id")
    r.add_argument("job_id")
    s = sub.add_parser("status", help="print a job record")
    s.add_argument("job_id")
    a = p.parse_args()
    if a.cmd == "dispatch":
        return dispatch()
    if a.cmd == "run":
        if not JOB_ID_RE.match(a.job_id):
            print(f"bad job id {a.job_id!r}", file=sys.stderr)
            return 2
        return _worker(a.job_id)
    rec = status(a.job_id)
    print(json.dumps(rec, indent=2) if rec else "null")
    return 0 if rec else 1


if __name__ == "__main__":
    raise SystemExit(_main())
