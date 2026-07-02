# server/ — the dictator stack

Everything the dictator runs. Python; services are the `ha-*` units in [`../systemd/`](../systemd/). Real
config lives in `../instance/` (git-ignored); templates in [`../config-examples/`](../config-examples/).

## Layout

| Dir/file | Role | ADR |
|----------|------|-----|
| `api/` | FastAPI **BFF** + endpoints; `viewmodel.py` = **single source of UI truth** (`METRIC_CATALOG`, control specs) rendered by both PWA and panel | 0013 |
| `web/` | The PWA (`app.js`) served at `/app` — a thin renderer of the BFF spec | 0013 |
| `ingest/` | MQTT → canonical readings; bridges (tasmota, levoit); `edge-mapper` (MAC→device/area) | 0001 |
| `storage/` | Two-tier: sqlite **hot** + parquet **archive** (compactor, hash manifest) | 0004,0006,0009 |
| `control/` | Actuator control loop, override/policy/scenes; trait-based | 0002,0011,0014 |
| `device_registry.py` | The registry the dictator owns (devices, traits, areas) | 0001,0002 |
| `cluster/` | Heartbeat / failover coordination (pairs with `../failover/`) | 0016,0018 |
| `comms/`, `mesh/` | Event/resource abstraction; mesh topology + relay-coverage assignment | 0012,0015 |
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
