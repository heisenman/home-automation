# ENVIRONMENT.md — machines, checkouts, and where things get built/flashed

Authoritative map of the three physical computers, so "which folder / which machine / where are the secrets"
never causes confusion again. This doc is in the repo, so it is present on **every** checkout. Surfaced from
the root [AGENTS.md](../AGENTS.md).

## The one trap: same repo, different folder name per machine

All three machines clone the **same GitHub origin** — `https://github.com/heisenman/home-automation.git` — but
the local **folder name differs**:

| Machine | Checkout path | `git remote` |
|---------|---------------|--------------|
| bench | `~/ha-coord` | origin = heisenman/home-automation |
| ha-dev (`.210`) | `~/home_automation` | same origin |

So a change committed+pushed from the bench (`~/ha-coord`) is picked up on `.210` with
`cd ~/home_automation && git pull`. **`ha-coord` does not exist on `.210`; `home_automation` does not exist on
the bench.** Same code, different directory — don't assume a path from one machine works on another.

## The three machines

### bench — `SuperDuperBuddy`, `192.168.0.112`
- **Checkout:** `~/ha-coord`. **ESP-IDF:** `~/esp/esp-idf` (present).
- **Role:** the **ops** box — the "convenient bench" where the Claude *ops* sessions do development. Physical
  access to bench hardware (reTerminal panels, programmers, pogo pins).
- **Builds/flashes:** the **reTerminal panels** (ESP32-P4 + C6). Panel dev tree is off-repo at
  `~/reterminal-dev/d1001-beachhead` (repo mirror: `provisioning/reterminal/beachhead/`), consuming the shared
  `firmware/components/` via `components/` symlinks. Panel OTA = plain HTTP server + `mosquitto_pub cmd/ota`.
- **Does NOT have** the edge nodes' real `secrets.h` — only `edge/*/main/secrets.example.h`. **Do not build or
  OTA edge nodes here:** the bin would compile with empty wifi creds + empty `HA_CMD_SECRET` (can't reconnect,
  can't be signed → `bad-sig`).

### ha-dev — `192.168.0.210` (the LIVE DICTATOR)
- **Checkout:** `~/home_automation`. **ESP-IDF:** `~/esp/esp-idf` (present).
- **Role:** the **canonical real-development** box (Hugh is migrating all real dev here) **and** the live
  dictator (MQTT broker, ingest, storage, BFF/PWA, automation, notify, keepalived MASTER).
- **Builds/flashes:** the **edge nodes** (`esp32c3`/`esp32c6`/`esp32s3-eth`) — build **and** signed OTA
  (`tools/edge_ota.py` + `edge_sign.py`). This is where each node's real `secrets.h` (wifi creds + per-device
  `HA_CMD_SECRET`) lives, so it's the only place an edge bin builds correctly and an OTA can be signed to match
  the running firmware. Edge builds link the shared `firmware/components/` via `EXTRA_COMPONENT_DIRS`.
- **Live-dictator production writes are CLASSIFIER-GATED** — hand Hugh copy-paste, never self-deploy (restart
  of existing `ha-*` services is fine; new package/unit installs are gated).

### fileserver — `192.168.0.245` (warm standby)
- **Role:** Hugh's **CRITICAL fileserver** + temporary HA warm-standby (keepalived BACKUP behind VIP `.200`).
- **⚠️ Never** a dev / deploy / optimization / host-config target. Touch **only** its `ha-*` guest services,
  nothing else on the box.

## Build / flash matrix (who owns what)

| Firmware | Built on | Flash / OTA | Secrets source |
|----------|----------|-------------|----------------|
| reTerminal **panel** (P4+C6) | **bench** (`~/reterminal-dev`) | HTTP + `mosquitto_pub d1001-beachhead/cmd/ota` (over-SDIO `cmd/slaveota` for the C6 NCP) | `provisioning/reterminal/beachhead/main/secrets.h` (bench) |
| **edge nodes** c3/c6/s3 | **`.210`** (`~/home_automation`) | `tools/edge_ota.py` (signed, `HA_CMD_SECRET`) or USB at the bench | `.210` `edge/<node>/main/secrets.h` |
| shared `firmware/components/` | both (linked, not flashed) | — | none (pure/no-secret modules) |

## Recipe: build + OTA an edge node (on `.210`)

```sh
cd ~/home_automation && git pull                     # get the latest committed change
cd edge/esp32c6 && . ~/esp/esp-idf/export.sh
idf.py set-target esp32c6   # first time only
idf.py build
cd ~/home_automation
export HA_CMD_SECRET="$(sed -nE 's/.*HA_CMD_SECRET[^"]*"([^"]*)".*/\1/p' edge/esp32c6/main/secrets.h)"
python3 tools/edge_ota.py --node <node-id> --bin edge/esp32c6/build/ha-edge-c6.bin \
        --serve-ip 192.168.0.210 --broker localhost
```

`HA_CMD_SECRET` must equal the secret compiled into the **running** firmware (same `secrets.h` that flashed it),
or the node rejects the directive with `bad-sig`. Sourcing it straight from `secrets.h` avoids typos.
