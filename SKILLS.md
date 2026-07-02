# SKILLS.md — how-to runbook index

The recurring procedures, so an agent finds the right recipe in one hop. Each entry points at the real doc/
tool that carries the steps. (Convention: ADR-0021. New runbook → add a row here.)

## Edge firmware / BLE

| Task | Runbook |
|------|---------|
| Build a new edge node (boots → relays → OTA-able) | [edge/FIRMWARE-GUIDE.md](edge/FIRMWARE-GUIDE.md) |
| Gated one-shot node bring-up | `tools/node_bringup.py` (see [tools/AGENTS.md](tools/AGENTS.md)) |
| Enroll a node / mint its command secret | `tools/enroll_node.py`, `tools/edge_sign.py` |
| OTA an edge node (signed, host-pinned, A/B) | `tools/edge_ota.py`; `ha_ota` module (FIRMWARE-GUIDE §OTA) |
| Pull BLE GATT history from a device | `tools/edge_pull_history.py`, `tools/edge_gatt.py` |

## reTerminal panels (ADR-0019)

| Task | Runbook |
|------|---------|
| Panel provisioning / factory-flash backup | [provisioning/reterminal/README.md](provisioning/reterminal/README.md) |
| BLE edge-node on the panel (plan + status) | [provisioning/reterminal/BLE-EDGE-NODE-PLAN.md](provisioning/reterminal/BLE-EDGE-NODE-PLAN.md) |
| **Serial-flash the C6 slave** (ESP-Prog + pogo) | [provisioning/reterminal/C6-SLAVE-FLASH-PROCEDURE.md](provisioning/reterminal/C6-SLAVE-FLASH-PROCEDURE.md) |
| Panel firmware OTA (over WiFi) | `d1001-beachhead/cmd/ota` (MQTT); C6 over WiFi via `cmd/slaveota` |

## Server / provisioning

| Task | Runbook |
|------|---------|
| Full server install (ISO → post-install) | [provisioning/01-bootstrap-iso.md](provisioning/01-bootstrap-iso.md) … `04-post-install.md` |
| Onboard a device (generic) | [docs/device-onboarding.md](docs/device-onboarding.md) |
| Levoit / Tasmota / OpenWRT intake | `provisioning/levoit/`, `docs/tasmota-s31-intake.md`, `provisioning/openwrt/` |
| Broker auth cutover / control go-live | `provisioning/broker-auth-cutover.md`, `provisioning/control-go-live.md` |

## Failover / cluster HA

| Task | Runbook |
|------|---------|
| Failover concepts + operations | [failover/README.md](failover/README.md), [failover/failover-runbook.md](failover/failover-runbook.md) |
| Rehearse a failover (reversible drill) | [docs/failover-drill.md](docs/failover-drill.md), `failover/failover-drill.sh` |
| History / parquet reconciliation | `failover/reconcile-history.sh`, `failover/reconcile-parquet.sh` (ADR-0016/0018) |
| Seed/provision a peer node | `failover/provision-peer.sh` |
| Cluster health check | `failover/cluster-doctor.sh` |

## Coordination

| Task | Runbook |
|------|---------|
| Two-Claude task board (ops/dev over MQTT) | `tools/agents/coord.py` — `--as <ops\|dev> list\|ready\|mine\|add\|note` |
| Delegate a bounded task to a fresh headless dev | `coord.py wake dev` (see [tools/AGENTS.md](tools/AGENTS.md)) |
