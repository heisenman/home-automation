"""admin_job — the detached maintenance-job record layer (id guard, atomic status, terminal error record).

The launch (`sudo systemctl start`) and the real orchestration are integration-tested on a live run; here we
cover the pure record logic the API polls and the worker's fail-safe (always leave a terminal record)."""
from server.maintenance import admin_job as J


def test_job_id_re():
    assert J.JOB_ID_RE.match("0123456789ab")
    assert not J.JOB_ID_RE.match("short")
    assert not J.JOB_ID_RE.match("0123456789abZZ")     # too long / uppercase
    assert not J.JOB_ID_RE.match("../etc/passwd")       # traversal can't masquerade as an id


def test_status_unknown_and_bad_id(tmp_path):
    orig = J.JOB_DIR
    J.JOB_DIR = tmp_path
    try:
        assert J.status("deadbeef0000") is None         # valid shape, no file
        assert J.status("nope") is None                 # bad shape rejected before any fs touch
    finally:
        J.JOB_DIR = orig


def test_write_status_roundtrip(tmp_path):
    orig = J.JOB_DIR
    J.JOB_DIR = tmp_path
    try:
        J._write("0123456789ab", {"job_id": "0123456789ab", "status": "running"})
        assert J.status("0123456789ab")["status"] == "running"
    finally:
        J.JOB_DIR = orig


def test_worker_bad_spec_leaves_error_record(tmp_path):
    """A spec that can't be turned into a plan must still leave a terminal 'error' record (never hang the
    poller on 'running'). Missing device_id hits the guard before any orchestration."""
    orig = J.JOB_DIR
    J.JOB_DIR = tmp_path
    try:
        J._write("0123456789ab", {"job_id": "0123456789ab", "status": "running", "spec": {"op": "rename"}})
        rc = J._worker("0123456789ab")
        rec = J.status("0123456789ab")
        assert rc == 2 and rec["status"] == "error" and "device_id" in rec["error"]
    finally:
        J.JOB_DIR = orig


if __name__ == "__main__":
    from tests._harness import run_module
    run_module(globals())
