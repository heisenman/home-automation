# ADR-0032 — Unregistered-device data quarantine

**Status:** Accepted (2026-07-10) — implemented; live deploy to ha-2 is a separate gated step.

**Directive origin:** Hugh, 2026-07-10, after the migrated-device silent data-drop incident (memory
`migrated-device-silent-drop`). "Instead of silently dropping telemetry from a device that is live-on-broker
but unregistered, write it to a **separate** DB for later merge-or-delete. Never fail silent." Data must be
kept in a **separate file** and is **never auto-deleted** — only an explicit purge or a successful merge
removes rows.

---

## Context

The ingest bridges translate a device's raw telemetry into the canonical `home/<area>/<device_id>/state`
message the writer persists. That translation needs a **registry** entry (device_id + area + device_type):

- `tasmota_bridge.py` — Tasmota `%topic%` → identity, from `instance/tasmota-devices.yaml`
- `levoit_bridge.py` — ESPHome node name → identity, from `instance/levoit-devices.yaml`
- `edge_mapper.py` — BLE MAC → identity, from `instance/devices.yaml`

**The failure mode (2026-07-10):** `airgap_router_pm` + `failover_pm` were migrated `.210 → ha-2`. The
config sync copied `.210`'s registry — where they were **commented out** with a `.210`-perspective
"deregistered here" note — verbatim to ha-2. ha-2 therefore never registered them. They published to ha-2's
broker for **~23 h** while `tasmota_bridge` logged `UNKNOWN topic` once and **dropped every message**. No
error, no alert — just a growing "no data" staleness. Those ~23 h are **lost** (only the on-device
cumulative `Total` kWh survived). This violates the project's primary directive
(memory `data-storage-is-primary`): storing sensor data is a first-class requirement of every server
activity.

The root cause has two independent fixes; this ADR is the **robustness net** for the second:

1. *Migration must ACTIVATE on the destination* (register the device in the destination registry, don't
   carry the source's deregistration). Process/tooling fix — tracked separately in `docs/FOLLOWUPS.md`.
2. *Never drop unregistered telemetry silently* — quarantine it instead. **This ADR.**

---

## Decision

A **quarantine sink** at each bridge drop-site. When a bridge sees telemetry from a device that is live on
the broker but not in the registry, instead of dropping it, it appends the raw message to a **separate
SQLite file**, `instance/db/quarantine.db` (NOT hot.db). A human (or a future admin op) later **merges**
(register → replay the captured readings into hot.db, recovering the window) or **purges** (junk) the device.

### Components

- **`server/ingest/quarantine.py`**
  - `QuarantineSink` (write side, held by a bridge): `capture(source, identity, topic, payload, metrics,
    …)` appends one row to `quarantined_readings` (raw payload **verbatim** + best-effort registry-
    independent canonical metrics) and upserts a per-device rollup in `quarantined_devices`. On the **first**
    sighting of a new identity it fires a one-shot `home/_alert/new` (`kind: live_but_unregistered`) if a
    publisher is wired in — so it never fails silent. **Never raises** into the bridge: a quarantine-DB
    failure logs once and the bridge drops exactly as before. Quarantine must not break live ingest.
  - `QuarantineStore` (read/merge/purge side, held by the CLI): `list_devices`, `readings`, `summary`,
    `merge`, `purge` — all returning report dicts (the `server/maintenance` convention) so the same core can
    back an admin API endpoint later.
- **Bridge hooks** — one branch each at the existing `UNKNOWN` site: `tasmota_bridge` (the incident path,
  wildcard `tele/+/…` subscription — high value), `edge_mapper` (wildcard `home/edge/+/+/adv`, genuinely
  receives + drops unknown MACs — high value), `levoit_bridge` (defense-in-depth: this bridge subscribes
  **per-registered-name**, so an unknown node is normally never received at all — its real risk is
  *non-subscription*, not a post-receipt drop; the hook is ready if that model ever broadens).
- **`tools/quarantine.py`** — the dictator's CLI: `list` / `inspect` / `merge` / `purge`.

### Merge is lossless recovery

`merge <source> <identity> --device-id … --area … --device-type …` reconstructs the canonical payload for
every **pending** captured reading and replays it into hot.db **through the writer's own `_insert_readings`**
(identical `INSERT OR IGNORE` idempotency, units, `device_last_seen` update). It then marks those rows
`merged` — it does **not** delete them (see retention). `--register` also appends the device to its source
registry (the *ACTIVATE-on-destination* fix), so future telemetry is ingested normally; without it, merge
prints the exact registry stanza to add by hand.

### Retention — never auto-delete (Hugh's directive)

There is **no retention cap, no eviction, no TTL**. Quarantined data is removed only by:
- a **successful merge** (rows marked `merged`; the data is now durably in hot.db), or
- an **explicit user `purge`** (requires `--yes`; `--merged` deletes only already-merged rows and keeps
  pending).

This is a deliberate, unbounded, additive store — consistent with `data-storage-is-primary`. Growth is
bounded in practice because a quarantined device should be promptly merged or purged (and the first-sighting
alert prompts exactly that).

### Safety / rollout

The change is **purely additive**: quarantine only ever captures what was *already* being dropped, and the
happy path (registered devices) is byte-for-byte unchanged. It is on by default (`--quarantine-db`, env
`HA_QUARANTINE_DB`, `''` disables) and degrades gracefully. Landing the code changes no live behavior for
registered devices. **Enabling it on the live ingest fleet (ha-2 + dev) is a separate, Hugh-gated deploy.**

---

## Consequences

- **No more silent loss.** A migrated/misconfigured device's data is preserved and recoverable rather than
  gone. Directly serves `data-storage-is-primary`.
- **Surfaced, not buried.** The first-sighting alert + `tools/quarantine.py list` make "live-but-
  unregistered" a visible, actionable state.
- **Separate file, separate concern.** quarantine.db never mixes with authoritative hot.db; a device only
  enters the record-of-record via an explicit, audited merge.
- **Later:** an admin-gated `POST /api/v1/admin/quarantine/{id}:merge|:purge` wrapping `QuarantineStore`
  (cf. the `ui-device-admin` followup); a periodic sanity sweep (`comm -23 <live LWT> <registered>`) that
  seeds quarantine proactively; the migration-ACTIVATE assertion (followup #1).

## Alternatives rejected

- **Roll-up marker only** (identity + first/last seen + count, no raw rows) — cheaper, but cannot *recover*
  the lost window, which is the entire point of the incident. Rejected.
- **Auto-register unknown devices into hot.db** — pollutes the record-of-record with junk/rotating-MAC
  tiles and guesses identity (area/type) wrong. Quarantine keeps the human/registry in the loop.
- **Retention cap / auto-evict** — contradicts the directive and `data-storage-is-primary`. Rejected.
