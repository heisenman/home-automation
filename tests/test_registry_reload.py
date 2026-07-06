"""server/ingest/registry_reload.py — the live registry-reload watcher (relocate-ingest-reload).

The ingest bridges cache the device registry at startup; a UI relocate edits the registry file but the
bridge kept stamping the OLD area so the device 'popped back'. RegistryReloader.current() must hand out
the freshest registry after the file changes — without a restart — while surviving a torn read (the
relocate rewrites the file non-atomically) and throttling stat() on a hot path.
"""
import logging

from server.ingest.registry_reload import RegistryReloader

# The torn-read test deliberately triggers a load failure; the reloader logs it via log.exception.
# Silence that expected traceback so the suite output stays clean (behaviour is asserted, not the log).
_QUIET = logging.getLogger("test.registry_reload.quiet")
_QUIET.addHandler(logging.NullHandler())
_QUIET.propagate = False


def _loader_factory(path):
    """A load_registry-shaped loader that parses 'k=v' lines into {k: {'area': v}}."""
    def load(p):
        out = {}
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ValueError(f"torn/invalid registry line: {line!r}")   # torn read -> raise
            k, v = line.split("=", 1)
            out[k] = {"area": v}
        return out
    return load


def _reloader(tmp_path, initial, throttle_s=0.0):
    f = tmp_path / "devices.reg"
    f.write_text(initial)
    return f, RegistryReloader(f, _loader_factory(f), throttle_s=throttle_s, logger=_QUIET)


def test_reloads_when_file_mtime_changes(tmp_path):
    f, r = _reloader(tmp_path, "meter1=kitchen\n")
    assert r.current()["meter1"]["area"] == "kitchen"
    # Simulate a relocate rewriting the registry (bump mtime explicitly — same-second writes must still
    # be seen; we use st_mtime_ns so a fresh write is a distinct value, but force it to be safe).
    f.write_text("meter1=office\n")
    _bump_mtime(f)
    assert r.current()["meter1"]["area"] == "office"   # picked up live, no restart


def test_throttle_suppresses_stat_until_interval(tmp_path):
    f, r = _reloader(tmp_path, "meter1=kitchen\n", throttle_s=60.0)
    assert r.current()["meter1"]["area"] == "kitchen"
    f.write_text("meter1=office\n")
    _bump_mtime(f)
    # within the throttle window the change is NOT observed
    assert r.current()["meter1"]["area"] == "kitchen"


def test_torn_read_keeps_previous_then_recovers(tmp_path):
    f, r = _reloader(tmp_path, "meter1=kitchen\n")
    assert r.current()["meter1"]["area"] == "kitchen"
    # relocate mid-write: file momentarily invalid (loader raises) -> keep previous value...
    f.write_text("meter1")           # no '=' -> loader raises
    _bump_mtime(f)
    assert r.current()["meter1"]["area"] == "kitchen"   # survived the torn read
    # ...and the NEXT poll after the write completes picks up the new value (mtime was NOT committed).
    f.write_text("meter1=office\n")
    _bump_mtime(f)
    assert r.current()["meter1"]["area"] == "office"


def test_missing_file_keeps_last_known(tmp_path):
    f, r = _reloader(tmp_path, "meter1=kitchen\n")
    assert r.current()["meter1"]["area"] == "kitchen"
    f.unlink()                       # stat() -> None: do not clobber the last-known-good registry
    assert r.current()["meter1"]["area"] == "kitchen"


def _bump_mtime(path):
    """Force a distinct mtime so a same-wall-second rewrite is observed deterministically in tests."""
    st = path.stat()
    import os
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


if __name__ == "__main__":
    from tests._harness import run_module
    run_module(globals())
