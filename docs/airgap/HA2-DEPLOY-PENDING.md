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
| _(none)_ | | | | | | |

**No items pending.** `adr0034-classify` was **deployed to ha-2 on 2026-07-11** (device-admin + taxonomy prod
cutover — [../coord/PROD-CUTOVER-RUNBOOK.md](../coord/PROD-CUTOVER-RUNBOOK.md)); verified on ha-2:
`classify('dehumidifier_living_room')` → `local-driver`. Marker + row retired together per the protocol above.

The cutover also deployed device-admin (`control.py`, `apply_rename_worksheet.py`, `admin_job.py`,
`ha-admin-job@.service`), the failover maintenance-inhibit (`failover/healthcheck.sh`), `model_project.py` +
`DEVICE-MODEL.md`, and the PWA — all verified by deployed-file checksum on ha-2, so none needs a tripwire row.
Deployed manifest recorded on ha-2 at `instance/cutover-deployed.txt`.
