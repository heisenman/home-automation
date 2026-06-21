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
        m = importlib.import_module(f"tests.{mod.name}")
        print(f"== tests.{mod.name} ==")
        rc |= run_module(vars(m))
    print("\nALL PASS" if rc == 0 else "\nFAILURES")
    return rc


if __name__ == "__main__":
    sys.exit(main())
