"""Tiny dependency-free test runner (no pytest — keeps the offline venv lean).

Each test module defines `test_*` functions and ends with:

    if __name__ == "__main__":
        from tests._harness import run_module
        run_module(globals())

Run one:   venv/bin/python -m tests.test_traits
Run all:   venv/bin/python -m tests.run_all
"""
from __future__ import annotations

import inspect
import os
import sys
import tempfile
import traceback
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def raises(exc_type):
    """Assert the block raises `exc_type` (like pytest.raises)."""
    try:
        yield
    except exc_type:
        return
    except Exception as e:  # wrong exception type
        raise AssertionError(f"expected {exc_type.__name__}, got {type(e).__name__}: {e}")
    raise AssertionError(f"expected {exc_type.__name__}, nothing raised")


_MISSING = object()


class MonkeyPatch:
    """The subset of pytest's `monkeypatch` fixture our suites use (setattr / setenv / delenv), undone after
    the test like pytest's. Without it, pytest-native tests failed here with a TypeError (missing argument)
    while passing under pytest — which read as flaky, order-dependent failures in run_all."""

    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value, raising=True):
        old = getattr(target, name, _MISSING)
        if old is _MISSING and raising:
            raise AttributeError(f"{target!r} has no attribute {name!r}")
        self._undo.append(lambda: delattr(target, name) if old is _MISSING else setattr(target, name, old))
        setattr(target, name, value)

    def setenv(self, name, value):
        old = os.environ.get(name)
        self._undo.append(lambda: os.environ.pop(name, None) if old is None else os.environ.__setitem__(name, old))
        os.environ[name] = str(value)

    def delenv(self, name, raising=True):
        if name not in os.environ:
            if raising:
                raise KeyError(name)
            return
        old = os.environ.pop(name)
        self._undo.append(lambda: os.environ.__setitem__(name, old))

    def undo(self):
        while self._undo:
            self._undo.pop()()


def run_module(ns: dict) -> int:
    tests = sorted(k for k, v in ns.items() if k.startswith("test_") and callable(v))
    name = ns.get("__name__", "tests")
    passed = failed = 0
    for t in tests:
        fn = ns[t]
        # Support the pytest fixtures our suites use, by parameter name: `tmp_path` gets a fresh temp dir (as a
        # Path) and `monkeypatch` a MonkeyPatch undone after the test, like pytest. Keeps pytest-native modules
        # (test_device_registry, test_device_push, test_required_services, …) runnable under this harness.
        params = inspect.signature(fn).parameters
        mp = MonkeyPatch()
        try:
            with tempfile.TemporaryDirectory() as d:
                kw = {}
                if "tmp_path" in params:
                    kw["tmp_path"] = Path(d)
                if "monkeypatch" in params:
                    kw["monkeypatch"] = mp
                fn(**kw)
            print(f"  PASS {t}")
            passed += 1
        except Exception:
            print(f"  FAIL {t}")
            traceback.print_exc()
            failed += 1
        finally:
            mp.undo()
    print(f"[{name}] {passed} passed, {failed} failed")
    rc = 1 if failed else 0
    if ns.get("__name__") == "__main__":
        sys.exit(rc)
    return rc
