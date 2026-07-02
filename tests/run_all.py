"""Run every test_*.py module. Usage: venv/bin/python -m tests.run_all"""
import importlib
import pkgutil
import sys

import tests
from tests._harness import run_module


def main() -> int:
    rc = 0
    for mod in pkgutil.iter_modules(tests.__path__):
        if not mod.name.startswith("test_"):
            continue
        print(f"== tests.{mod.name} ==")
        try:
            m = importlib.import_module(f"tests.{mod.name}")
        except Exception as e:
            # Don't let one unimportable module (e.g. a missing dep outside the server venv)
            # abort the whole suite and hide every result after it.
            print(f"  ERROR importing tests.{mod.name}: {e!r}")
            rc |= 1
            continue
        rc |= run_module(vars(m))
    print("\nALL PASS" if rc == 0 else "\nFAILURES")
    return rc


if __name__ == "__main__":
    sys.exit(main())
