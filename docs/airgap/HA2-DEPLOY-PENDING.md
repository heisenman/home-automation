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

---

## ✅ 2026-08-02 — SGP41 intake/flash surface deployed to ha-2 (the last half of `7c65a9f`)

`7c65a9f` (SGP41 support) shipped in two halves, and only one had reached ha-2. The **banding** half —
`gas_compensation.py`, `viewmodel.py` — went out 08-01 with the unified air-quality work. The **intake /
flash / backfill** half did not, and it was left as the open item in
[../RESUME-2026-08-01b-sgp41-and-board-sweep.md](../RESUME-2026-08-01b-sgp41-and-board-sweep.md) ("wants its
own VIP-inhibit window").

**How it surfaced.** Hugh reflashed a board as `sgp41_mech`; the node came up correctly and announced
`abilities=["sgp41_gas"]`, but prod intake answered:

> node 'sgp41_mech' announced no gas ability (['sgp41_gas']) and has no registry record.

That message is `_register_edge_node_device`'s **relay-only** branch, reached because ha-2's
`control.py` still had a four-entry `_GAS_CAPABILITIES` with no `sgp41_gas` key. Note the failure mode:
the node was *fine*, the message was *coherent*, and it named a real design rule (a relay node genuinely
needs no device record) — so it reads as a verdict about the hardware rather than as a stale-deploy
symptom. **An unknown ability falling through to "you must be a relay" is the trap**; the branch cannot
distinguish "no gas ability" from "a gas ability this build has never heard of."

Deployed (checksum-verified on ha-2 against this checkout, all six identical):

| file | what was missing on ha-2 |
|------|--------------------------|
| `server/api/control.py` | `sgp41_gas` in `_GAS_CAPABILITIES` — **the blocker** |
| `server/ingest/edge_discovery.py` | `sgp41` → `sgp41_gas` in `_SENSOR_ABILITY` |
| `server/maintenance/edge_flash.py` | `sgp41` in `GAS_CHOICES` |
| `server/maintenance/gas_quality_persist.py` | `_backfill_sgp41` (VOC+NOx pairing) + `FAMILY_RAW` gate |
| `server/maintenance/recompute_air_quality.py` | explicit `sgp41` raw-signal match |
| `server/web/app.js` | `nox_index`/`nox_raw` catalog rows + `sgp41_gas` in the gas list |

The two already-deployed files diffed **0 lines** and the other six differed **only** in their SGP41
additions — no reverse drift on ha-2, so this was a clean forward-only copy.

**VIP window.** `instance/.maintenance-fit` touched → `sudo systemctl restart ha-api ha-api-tls` →
flag removed (`failover/healthcheck.sh` §0, `MAINT_FIT_MAX_AGE=300`). VIP `192.168.1.200` held on ha-2
across the restart; `/health` answered `hot_rows=497428` immediately after.

**Verified live on ha-2, not inferred from the commit:**

    # the gate that produced the message now passes, against the REAL registry
    venv/bin/python3 -c "from server.api.control import _register_edge_node_device, DORMANT_AREA; \
      print(_register_edge_node_device('instance/devices.yaml','sgp41_mech',['sgp41_gas'],DORMANT_AREA,dry_run=True))"
    # -> ('gas_staging', None)     [was: (None, 'announced no gas ability …')]

and the live discovery cache offers exactly one candidate — `sgp41_mech`, `['sgp41_gas']`, online,
`fw=v27-sgp41id`, `enrolled=false`.

### Gotcha found while verifying: the reflashed board leaves a **ghost hello**

`sgp41_mech` and the retired `sgp40_stdby3` share MAC `10:bd:a3:a0:96:bc` — same physical board, reflashed
under a new node_id. Both hellos sit **retained** on the broker, so `home/edge/+/hello` shows two nodes for
one board. This is harmless *only* because `EdgeDiscoveryCache.candidates()` filters on the retained
`status` LWT, not on message recency: `sgp40_stdby3` is `offline`, so it is excluded. Anything that reads
hello topics **directly** would double-count the board. Re-provisioning a node under a new id does not
retract its old retained hello.

---

## ✅ 2026-08-01 — firmware tree synced to ha-2 (board `sync-firmware-tree-to-ha2`)

ha-2 originates C6 fleet OTAs (nodes pin `ota_host` at `.1.210`, memory `c6-fleet-ota-from-ha2`), but only
SERVER code had been deployed there. Its firmware tree was pre-Phase-0 and **missing five whole
components** — `ha_eth`, `ha_gas`, `ha_led`, `ha_wifi`, `sgp41` — i.e. no gas lane at all, and
`tools/edge_ota.py` had no `generic@<target>` gate, so it would have **rejected a generic image**.

Synced the **git-tracked** files under `firmware/` + `edge/` + `tools/edge_ota.py` (165 files, whole-tree
sha256 verified identical). Git-tracked deliberately: it excludes `secrets.h` (gitignored, per-node
identity) and `build/` artifacts. **No `secrets.h` exists on ha-2 and the sync did not create one** —
node identity must never be copied between boxes (ADR-0020 anti-cross-provisioning).

No VIP window was needed: nothing on ha-2 *executes* from these paths. The only references in
`ha-edge-history.service` / `ha-edge-mapper.service` are `Documentation=` lines.

### ⚠ Correction to a common assumption: **ha-2 cannot BUILD firmware**

There is no ESP-IDF on ha-2 and no build tree. "ha-2 originates OTAs" means it **serves and triggers**
them, not that it compiles them. The real workflow (already in memory `c6-fleet-ota-from-ha2`) is:

    build on .210  →  scp the .bin to ha-2  →  run tools/edge_ota.py ON ha-2

So this sync makes ha-2's tree *consistent and readable* (module matrix, nodes.yaml manifest for the
identity gate, edge_ota.py able to accept a generic image) — it does **not** make ha-2 self-sufficient for
firmware builds, and installing an IDF there is not currently intended.
