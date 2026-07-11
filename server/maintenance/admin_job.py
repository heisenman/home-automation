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
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
JOB_DIR = REPO_ROOT / "instance" / "db" / "admin-jobs"
JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")            # our own ids; also the guard on the systemctl arg
_UNIT = "ha-admin-job@{}.service"


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
    """Record a 'running' job and start its detached unit. ``spec`` = {op: rename|relocate, device_id,
    new_id?, new_area?, restamp?}. Raises on a failed launch (caller maps to HTTP 500)."""
    job_id = uuid.uuid4().hex[:12]
    _write(job_id, {"job_id": job_id, "status": "running", "spec": spec, "started": _now()})
    # `systemctl start` on a Type=oneshot BLOCKS until the job finishes, and the job runs ~20-40s AND restarts
    # ha-api itself — so we must NOT block the caller (the API request / the whole detached rationale). Fire
    # it in its own session and return immediately; the unit runs in its own cgroup (survives the ha-api
    # restart it triggers) and reports through the durable JSON the caller polls. (Can't use --no-block: the
    # NOPASSWD rule matches exactly `systemctl start ha-*`, no extra flag.)
    try:
        subprocess.Popen(["sudo", "systemctl", "start", _UNIT.format(job_id)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        rec = {"job_id": job_id, "status": "error", "spec": spec, "started": _now(),
               "error": f"launch failed: {e}"}
        _write(job_id, rec)
        raise RuntimeError(rec["error"])
    return job_id


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
    r = sub.add_parser("run", help="run a recorded job (invoked by the systemd unit)")
    r.add_argument("job_id")
    s = sub.add_parser("status", help="print a job record")
    s.add_argument("job_id")
    a = p.parse_args()
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
