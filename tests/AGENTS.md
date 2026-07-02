# tests/ — server-logic guards

Python test suites for the dictator stack. Run before proposing server changes.

```sh
python3 tests/run_all.py          # full suite (use the server venv)
```

## What's guarded (representative)

| Suite | Guards |
|-------|--------|
| `test_viewmodel.py` | **BFF = single UI truth** — PWA/panel render the same `METRIC_CATALOG`/control specs (ADR-0013) |
| `test_module_matrix.py` | **Firmware matrix = build truth** — `edge/MATRIX.md` matches what each `CMakeLists` links (ADR-0020) |
| `test_control_api.py`, `test_controller.py`, `test_control_store.py`, `test_automation.py` | Control loop, override/policy, automation (ADR-0011/0014) |
| `test_device_registry.py` | Registry + traits (ADR-0002) |
| `test_auth_tokens.py` | JWT / token auth (ADR-0017) |
| `test_edge_mapper_dedup.py` | MAC→device mapping + dedup (ADR-0001) |
| `test_mesh_*.py`, `test_gap_watcher.py` | Mesh topology/assignment/rate, gap watching (ADR-0015) |
| `test_comms_events.py` | Comms/events abstraction (ADR-0012) |
| `test_aranet.py`, `test_midea_driver.py`, `test_loop.py` | Device drivers, loop |

## Discipline

- A new invariant (especially a **code-backed doc**, e.g. a generated `edge/MATRIX.md` under ADR-0020) should
  get a test that fails when the doc and the code disagree — same pattern as `test_viewmodel.py`.
- Keep suites green before committing server changes; note skipped/failing tests honestly in the checkpoint.
