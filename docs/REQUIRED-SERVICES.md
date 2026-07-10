# Required services — provisioning checklist + supervisor source-of-truth

> **Why this exists (2026-07-10):** the Levoit purifier vanished from both panels for **~14h**. Root cause:
> `ha-levoit-bridge` was **never installed on ha-2** — the registry was synced but the *service* wasn't. When
> the device repointed to ha-2's broker, its telemetry arrived with no mapper to translate it. systemd
> faithfully runs what's installed, but has **no concept of what *should* be installed for this host's role**.
> This document is that concept: the authoritative per-role list of required units, used **(a)** as a
> provisioning checklist when standing up a server and **(b)** as the source-of-truth for the **services
> supervisor** (below) that alerts when a required unit is missing/inactive. Same failure class as the PM-gap
> and the E1001 SHT40: *activate-on-destination*.

Verify a host against its role: every unit marked **must=active** should be `systemctl is-active` = active
(services) or its `.timer` active (periodic jobs). A missing unit-file (`is-enabled` = `not-found`) is the
dangerous case — it looks like nothing is wrong.

---

## Host roles

| Role | Host (today) | What it is |
|---|---|---|
| **dictator** | ha-2 (`192.168.1.210`, air-gapped) | the production record-of-record + control loop |
| **dev / web-bridge** | `.210` (`192.168.0.210`) | dev platform + permanent HTTPS bridge to ha-2 |
| **failover-standby** | `.210` `~/ha-airgap-standby` (`ha-ag-*`) | warm mirror of the dictator; seizes VIP on failover |
| **air-gap gateway** | `.210` (`ha-relay-*`, firewall) | controlled internet pass-through for the air-gapped net |

A single box can hold several roles (`.210` holds three). "Required" = the union of its roles' sets.

---

## A. Core stack — every HA server (dictator + standby)  ·  must=active (long-running services)
| Unit | Purpose | Notes |
|---|---|---|
| `ha-writer` | subscribes `home/+/+/state` → hot.db | the sink; nothing is recorded without it |
| `ha-api` | read + control API (`:8123`) | control routes only mount on the VIP holder |
| `ha-edge-mapper` | edge BLE advert → canonical `home/…/state` | required if any ESP edge nodes |
| `ha-edge-history` | edge history backfill ingest | |
| `ha-scanner` | onboard BLE scan → canonical | required if the host has a local BLE radio |
| `ha-tasmota-bridge` | Tasmota `tele/#` → canonical | required if any Tasmota device (plugs/PMs) |
| `ha-levoit-bridge` | Levoit/ESPHome `<node>/#` → canonical | **required if any ESPHome purifier — the unit that was MISSING** |
| `ha-controller` | automation control loop | **dictator only**; on the standby it's installed but **gated OFF** (VRRP) |

## B. Maintenance timers — every HA server  ·  must=active (the `.timer`; the `.service` is `static`)
| Timer | Purpose |
|---|---|
| `ha-compactor.timer` | hot.db → parquet compaction |
| `ha-rollup.timer` | multi-resolution rollup ladder (ADR-0022) |
| `ha-verify-hashes.timer` | parquet hash-manifest verify (ADR-0004) |
| `ha-gap-watcher.timer` | detects data gaps / stale devices |
| `ha-pending-sweeper.timer` | clears expired pending-holds (ADR-0028) |
| `ha-gas-quality-sampler.timer` | derived air-quality metrics | *(if gas sensors)* |
| `ha-power-sampler.timer` | power/idle sampling |
| `ha-router-reconcile.timer` | router-config reconcile *(air-gap net)* |

## C. Cluster / failover — any clustered host  ·  must=active
| Unit | Purpose |
|---|---|
| `keepalived` | VRRP VIP (`.1.200` air-gap / `.200` household) |
| `ha-cluster-heartbeat` | peer heartbeat over the bus |
| `ha-reconcile-history` | bidirectional hot.db reconcile loop (ADR-0016) |
| `ha-relay-coordinator` | actuator-relay coordinator, **VRRP-gated** (active only on VIP holder) |

## D. Role substitutions — internet vs air-gap  ⚠️ these DIFFER by role, don't cross-install
| Need | Internet-connected host (dev/bridge) | Air-gapped dictator (ha-2) |
|---|---|---|
| Weather | `ha-weather.timer` (direct open-meteo) | `ha-relay-weather.timer` (request via gateway) — **direct one DISABLED** |
| Push alerts | `ha-ntfy-bridge` (direct ntfy) | `ha-relay-alert-egress` (via gateway) — **direct one absent** |
| TLS web | `ha-api-tls` (HTTPS bridge `:8443`) | *(none — ha-2 serves plain `:8123`; the bridge does TLS)* |

## E. Air-gap gateway (`.210` only)  ·  must=active
`ha-relay`, `ha-relay-broker`, `ha-relay-forwarder`, `ha-airgap-bridge` (pass-through), `ha-cert-monitor.timer`,
`ha-sneakernet-nag.timer`. Oneshots (apply-then-exit, so `inactive` is normal): `ha-airgap-firewall` (nftables
default-deny), `ha-break-glass@` (USB recovery, template). Defs live in `provisioning/airgap/`, **not** `systemd/`.

## F. Failover-standby stack (`.210` `~/ha-airgap-standby`, `ha-ag-*`)  ·  must=active
`ha-ag-mosquitto`, `ha-ag-writer`, `ha-ag-api`, `ha-ag-edge-mapper`, `ha-ag-edge-history`, `ha-ag-tasmota-bridge`,
`ha-ag-reconcile-parquet.timer`. **Gated OFF** (active only on VIP seizure): `ha-ag-controller`,
`ha-ag-relay-coordinator`. (Runs on its own isolated venv — [[shared-venv-esphome-paho]].)

---

## Provisioning checklist — standing up a new server
1. Determine the host's **role(s)** (§Host roles) → its required set = union of those sections.
2. Install units: `sudo ./install.sh` copies `systemd/*.service|*.timer`; air-gap units come from
   `provisioning/airgap/*/install*.sh`. **Verify each required unit-file exists** (`is-enabled` ≠ `not-found`).
3. `sudo systemctl enable --now <unit>` for every must=active unit; leave gated units (`ha-controller` on a
   standby, VRRP-gated relays) installed-but-disabled.
4. Place `instance/` config-of-record (`devices.yaml`, `levoit-devices.yaml`, `tasmota-devices.yaml`,
   `control.yaml`, `mqtt.env`, secrets) — **a registry without its bridge service records nothing.**
5. Apply role substitutions (§D): air-gap → relay-weather/alert-egress, **disable** direct weather/ntfy.
6. **Run the supervisor once** (`--check`) → it must report zero missing/inactive required units.
7. Verify data actually flows: every registered device has a fresh `device_last_seen`; alerts route out.

---

## The services supervisor (design — to build)
**Goal:** make "a required service is missing" a *loud* condition, not a silent 14h gap. systemd supervises
*installed* units; the supervisor supervises the *manifest*.

- **Manifest** (machine-readable source of truth, derived from this doc): `provisioning/required-services.yaml`
  — `role → [units]`, each `{must: active|gated|oneshot, if: <capability>}`. This doc and the manifest are
  kept in sync (the manifest is what the supervisor reads).
- **`ha-supervisor.timer`** (every ~5 min, on each host): resolves the host's roles, and for each required
  unit checks `is-enabled` (catches **never-installed** — the levoit case) and `is-active` (catches
  crashed/stopped). Any gap → publish `home/_alert/new` (`kind: service_missing`, severity by unit
  criticality) → routed to ntfy via the bridge/relay. Emits a retained `home/_supervisor/<host>/status`
  (green + last-check ts + gap list).
- **Mutual back-check (your point):** the supervisor must not be a silent single point of failure. Two levels:
  1. it includes **itself** in the manifest (a host must run its own `ha-supervisor.timer`); and
  2. **`cluster-doctor` (cross-node) verifies every node's `home/_supervisor/<host>/status` is FRESH** — a
     stale/absent supervisor heartbeat is itself a FAIL. So local units are watched by the supervisor, and the
     supervisors are watched by the peer/cluster-doctor. Neither can die unnoticed.
- **Non-destructive:** the supervisor **reports/alerts only** — it does not auto-start units (a missing unit
  may be an intentional role difference; auto-starting could fight a gated unit). Remediation stays a human/
  provisioning step, prompted by the alert.

**Relation to existing tooling:** `failover/cluster-doctor.sh` already does cross-node health (VIP, brokers,
archive parity). The supervisor is the per-host unit-completeness layer beneath it; cluster-doctor gains the
"supervisor heartbeat fresh on every node" assertion. Board task: `services-supervisor`.
