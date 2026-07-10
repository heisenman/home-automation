# HA2-DEPLOY-PENDING.md — committed to git, NOT yet deployed to ha-2 (air-gap prod)

**The air-gap means `git-committed ≠ deployed-on-ha-2.`** Production (`ha-2`) is physically air-gapped; code
arrives by a deliberate scp step, not by `git push` ([[airgap-checkout-drift]]). So a merged commit can be
*live in this checkout* while ha-2 still runs the **old** code. That gap is fine to carry **as long as it is
visible** — this file is where it's made visible.

## How the tripwire works (why you're reading this)

Each committed-but-undeployed change plants a **`HA2-DEPLOY-DRIFT:<id>`** marker at its code site **and** a row
below. If future work runs up against one of these — most dangerously, someone hitting a symptom that is
**already fixed here but not on ha-2** — the marker routes them straight to this ledger: **deploy it, do not
re-implement it** (that re-implementation is the "unforced refactor error" this system exists to prevent).

`tests/test_ha2_deploy_pending.py` keeps the two in sync: a marker with no ledger row (or a row with no marker)
**fails the suite**. So an item can only leave this list by removing *both* — which is exactly what "I actually
deployed it to ha-2" should look like.

## Resolving an item (deploy to ha-2)

1. scp the listed path(s) into ha-2's checkout (see [AIRGAP-MIGRATION.md](AIRGAP-MIGRATION.md) for the sync
   procedure); **verify the DEPLOYED file on ha-2**, don't trust the commit ([[airgap-checkout-drift]]).
2. Run the row's **verify** command *on ha-2* to confirm the new behavior is live.
3. Remove the `HA2-DEPLOY-DRIFT:<id>` marker from the code **and** delete the row here, in one commit.

## Pending items

| id | path(s) | marker | committed | trigger — when this MUST deploy | verify on ha-2 | remedy |
|----|---------|--------|-----------|--------------------------------|----------------|--------|
| `adr0034-classify` | `server/maintenance/device_push.py` | `HA2-DEPLOY-DRIFT:adr0034-classify` | `10235b8` | a **device migration or failover-rebuild run *from ha-2*** re-pushes devices — a LAN actuator (`node: server`: Midea/Levoit/host) would hit the **old** `classify()`, return `unknown`, and **abort** (the bug ADR-0034 Phase 2 already fixed). | `python -c "from server.maintenance import device_push as D; print(D.classify('dehumidifier_living_room'))"` → must print `local-driver` | scp `device_push.py` to ha-2; re-run verify |

### Detail — `adr0034-classify`

ADR-0034 Phase 2 (commit `10235b8`) re-framed `device_push.classify()` from the flat
`{tasmota,esp32,ble,unknown}` enum into the Node/transport-plane enum
`{mqtt-broker,edge-signed,ble-passive,local-driver}` and added the **`local-driver`** class so LAN actuators
migrate (skip repoint → confirm → pending-hold) instead of aborting at `unknown`.

- **Why deferral is safe today:** `classify()` is reached **only** through the manual `device_push <id>`
  migration entrypoint. No running service executes it (`pending_sweeper` imports `device_push` but calls only
  `drop_cleanup`, which is unchanged; ingest/control/storage/BFF don't import it). So ha-2 behaves identically
  whether or not it has this commit — until someone *runs a migration on ha-2*.
- **The one scenario that forces deployment:** a failover-rebuild (or any device migration) executed **from
  ha-2's checkout**. Then the stale `classify()` re-appears and LAN actuators abort. If you see that: it is
  **already fixed** — deploy `device_push.py`, don't re-solve `device-push-actuator-class`.

*Related:* [ADR-0034](../adr/ADR-0034-device-object-model-node-ability-entity.md) · [FOLLOWUPS](../FOLLOWUPS.md)
· failover-rebuild is the trigger scenario — see [failover-drill.md](../failover-drill.md).
