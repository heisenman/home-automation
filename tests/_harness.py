"""Tiny dependency-free test runner (no pytest — keeps the offline venv lean).

Each test module defines `test_*` functions and ends with:

    if __name__ == "__main__":
        from tests._harness import run_module
        run_module(globals())

Run one:   venv/bin/python -m tests.test_traits
Run all:   venv/bin/python -m tests.run_all
"""
from __future__ import annotations

import sys
import traceback
from contextlib import contextmanager


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


def run_module(ns: dict) -> int:
    tests = sorted(k for k, v in ns.items() if k.startswith("test_") and callable(v))
    name = ns.get("__name__", "tests")
    passed = failed = 0
    for t in tests:
        try:
            ns[t]()
            print(f"  PASS {t}")
            passed += 1
        except Exception:
            print(f"  FAIL {t}")
            traceback.print_exc()
            failed += 1
    print(f"[{name}] {passed} passed, {failed} failed")
    rc = 1 if failed else 0
    if ns.get("__name__") == "__main__":
        sys.exit(rc)
    return rc
