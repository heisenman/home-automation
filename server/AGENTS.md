# server/ — the dictator stack

Everything the dictator runs. Python; services are the `ha-*` units in [`../systemd/`](../systemd/). Real
config lives in `../instance/` (git-ignored); templates in [`../config-examples/`](../config-examples/).

*↑ The by-location node for `server/` in the [root AGENTS.md](../AGENTS.md) tree (ADR-0021/0025); the
by-capability index is [`docs/REUSE.md`](../docs/REUSE.md). Link up, don't duplicate.*

## Layout

| Dir/file | Role | ADR |
|----------|------|-----|
| `api/` | FastAPI **BFF** + endpoints; `viewmodel.py` = **single source of UI truth** (`METRIC_CATALOG`, control specs) rendered by both PWA and panel | 0013 |
| `web/` | The PWA (`app.js`) served at `/app` — a thin renderer of the BFF spec | 0013 |
| `ingest/` | MQTT → canonical readings; bridges (tasmota, levoit); `edge-mapper` (MAC→device/area, **+ reach census `home/edge/+/reach` → `record_link(ok=None)`**) | 0001,0023 |
| `storage/` | Two-tier: sqlite **hot** + parquet **archive** (compactor, hash manifest) | 0004,0006,0009 |
| `control/` | Actuator control loop, override/policy/scenes; trait-based | 0002,0011,0014 |
| `device_registry.py` | The registry the dictator owns (devices, traits, areas) | 0001,0002 |
| `maintenance/` | `device_migrate.py`: idempotent device **rename/retire across EVERY store on both boxes** (registry/hot/parquet/rungs/ntfy) — guards the `reconcile-history` merge from resurrecting a deleted id; pure `run_migration` report-dict backend for a future admin control. Runbook: [`../docs/runbook-device-migrate.md`](../docs/runbook-device-migrate.md) | 0016,0022 |
| `cluster/` | Heartbeat / failover coordination (pairs with `../failover/`) | 0016,0018 |
| `comms/`, `mesh/` | Event/resource abstraction; mesh topology + relay-coverage assignment; `coordinator.py` also **pushes** the reach-census trigger (`home/edge/<node>/reach/req`, signed, sig-only) each pass | 0012,0015,0023 |
| `notify/` | Alert engine → MQTT `home/_alerts` → ntfy bridge (web-push dropped) | — |
| `weather/` | Weather lane | 0008 |
| `config/`, `util/` | Config loading, shared helpers | — |

## Contracts & gotchas

- **BFF is the single UI-truth source** — add UI decisions to `viewmodel.py`; PWA + panel both render them.
  Don't hardcode metric/control lists in a client. Tests pin this: `../tests/test_viewmodel.py`.
- **Auth (ADR-0017):** `:8123` = `ha-api` (LAN reads open); `:8443` = `ha-api-tls` with `/auth/login` JWT.
  Admin credential = `SHA256("ha-api:"+master)`; raw master never crosses the wire.
- **Live-dictator writes are gated** (see root AGENTS.md): restart existing `ha-*` = fine; new units/packages
  or new code = hand Hugh, don't self-deploy.
- Run tests before proposing server changes: `python3 ../tests/run_all.py` (venv).
