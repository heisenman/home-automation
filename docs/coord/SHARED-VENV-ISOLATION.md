# Shared-venv isolation — keep one instance's `pip install` from breaking another's services

> **Reusable co-resident tooling.** This host runs multiple Claude instances (`dev`, `dev2`, …) plus the
> long-running `ha-*` systemd services, and they **share one Python virtualenv** (`venv/` at the repo root).
> A dependency change by any one of them lands in everyone's interpreter. This doc is the hazard + the
> fix-recipe. Control plane: the coord board. See [HOST-COORD.md](HOST-COORD.md).

## The hazard (why this exists)

`venv/` at the repo root is a **single shared virtualenv**. Every co-resident instance and every `ha-*`
service imports from it. So:

- One instance runs `pip install esphome` → esphome pins **`paho-mqtt<2`** → pip **downgrades** the shared
  `paho-mqtt` from 2.x to 1.6.1.
- The server code (`server/storage/writer.py`, the whole `ha-*` fleet) constructs
  `mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)` — a **paho-v2-only** API.
- The next time any of those services **restarts**, it dies on
  `AttributeError: module 'paho.mqtt.client' has no attribute 'CallbackAPIVersion'`.

The break is **silent until restart**: the running process keeps its already-imported modules, so nothing
looks wrong until a deploy, a reboot, or a failover restarts the service onto the poisoned venv. That is the
worst kind of latent failure for a **failover** target — it looks healthy right up until you need it.

This has bitten us **twice** (2026-07-08, 2026-07-10), both times from an esphome install in the shared venv.
Memory: `shared-venv-esphome-paho`.

## Two defenses (use both)

### 1. Make MQTT tools tolerate v1 **and** v2 (already done for edge tools)

`tools/edge_ota.py` and `tools/repoint_node.py` now `try` the v2 constructor and fall back to v1. Any **new**
MQTT helper an instance writes must do the same — never hardcode `CallbackAPIVersion`. The `ha-*` **services**
deliberately do NOT do this (they require v2 and must fail loudly if the venv is wrong) — so defense #2 is what
protects them.

### 2. Give a critical service its **own** isolated venv (esphome-proof)

Any service that must **not** be at the mercy of another instance's `pip install` gets a private venv built
from the pinned `requirements.txt`. This is what the **air-gap failover** (`~/ha-airgap-standby`) now uses —
its venv was previously a symlink to the shared `venv/`, inheriting the whole hazard.

**Recipe** (build to a temp dir, verify, atomic swap, restart onto it — so running services aren't disrupted
mid-build):

```bash
TARGET=~/ha-airgap-standby            # the isolated instance's root
cd "$TARGET"

# 1. Build a fresh, real venv ALONGSIDE the current one (don't disturb running services)
rm -rf venv.new
python3 -m venv venv.new
venv.new/bin/pip install --upgrade pip
venv.new/bin/pip install -r ~/home_automation/requirements.txt     # pinned; offline: --no-index --find-links wheelhouse/

# 2. Verify the thing that actually breaks — construct a paho v2 client + import the hot deps
venv.new/bin/python - <<'PY'
import paho.mqtt.client as m
m.Client(m.CallbackAPIVersion.VERSION2)          # proves paho v2 is present (the recurring break)
import fastapi, uvicorn, pyarrow, duckdb, yaml, numpy, bleak, cryptography
from importlib.metadata import version
print("paho", version("paho-mqtt"), "— all imports OK, v2 client constructed")
PY

# 3. Atomic swap: replace the symlink/olddir with the real venv (SAME path, so unit ExecStart lines are unchanged)
rm venv && mv venv.new venv

# 4. Restart the service(s) onto it and confirm they come back healthy
for u in ha-ag-mosquitto ha-ag-writer ha-ag-api ha-ag-edge-mapper ha-ag-edge-history ha-ag-tasmota-bridge; do
  sudo systemctl restart "$u"
done
systemctl list-units 'ha-ag-*' --state=failed --no-legend || echo "(none failed)"
```

**Why same-path swap:** the systemd units point `ExecStart` at `$TARGET/venv/bin/python`. Building at
`venv.new` then `mv`-ing over `venv` keeps that path stable — no unit edits, and the swap is a single rename.

**Verify functionally, not just `is-active`:** the paho-v2 failure is a *startup crash → auto-restart* loop, so
also check the service actually did its job — e.g. the writer logs `MQTT connected` (no `AttributeError`), and
the API answers a real route (`curl -s -o /dev/null -w '%{http_code}' localhost:8124/api/v1/rooms` → `200`).

## esphome: use the docker wrapper (canonical)

esphome runs via **docker only** — `tools/esphome` wraps `ghcr.io/esphome/esphome:latest` so its
`paho-mqtt<2` pin never touches the shared `venv/`. `visko` is in the `docker` group (sudo-less). Run it from
the config directory:

```bash
cd provisioning/reterminal/e1001
../../../tools/esphome config  e1001.yaml                       # validate
../../../tools/esphome compile e1001.yaml                       # build
../../../tools/esphome upload  e1001.yaml --device 192.168.1.131  # OTA
HA_ESPHOME_DEVICE=/dev/ttyUSB0 ../../../tools/esphome run e1001.yaml  # cable flash
```

esphome was removed from the shared `venv/` on 2026-07-10 (`pip uninstall esphome`; `pip check` clean, paho
stays 2.1.0, no ha-* restart needed — services never imported it). **Do not `pip install esphome` back into
`venv/`.**

## Standing rule for co-resident instances

- **esphome does not belong in the shared `venv/`.** Use `tools/esphome` (docker). It pins
  `paho-mqtt<2` and will poison every `ha-*` service the moment one restarts.
- If a Python tool suddenly `AttributeError`s on a stdlib-looking import, **suspect a co-resident `pip install`**
  changed the shared venv. Check `venv/bin/pip freeze | grep paho` first.
- Before installing anything into the shared venv, consider whether it belongs there at all — or in an isolated
  venv per defense #2.
