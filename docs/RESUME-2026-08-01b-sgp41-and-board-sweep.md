# RESUME — 2026-08-01 (session B): SGP41 support + a board sweep

**Seat:** dev2 (interactive with Hugh). **Tree:** clean, `HEAD == origin/main`.
**Prior session the same day:** `docs/RESUME-2026-08-01-pwa-firmware-flashing.md` (PWA firmware flashing).

---

## Headline: Hugh's board really was an SGP41, and nothing could have told him

He suspected from the **product listing** that a board configured as SGP40 was actually an SGP41. It was.
Our autodetect could never have caught it, and the reason generalises:

* SGP40 and SGP41 both ACK at I²C **0x59** and both pass the same `0x280E` self-test.
* Worse, and contrary to both datasheets' command tables: **an SGP41 answers the SGP40's `0x260F`
  measure_raw with CRC-valid data.** So a misidentified SGP41 looks *completely healthy* — correct-looking
  VOC numbers, self-test passing, nothing anomalous — and **silently never reports NOx.** This board had
  been doing exactly that for days. You lose half the sensor with no error anywhere.
* The featureset word on the real part was **`0x0240`, not `0x0040`** — an exact-match check (what several
  libraries do) misidentifies it. Mask to the low 9 bits. That is esphome/issues#5995.

Identification is now **featureset-first** (`0x202F`, masked), with a behavioural probe as fallback whose
**order is load-bearing** (`0x2619` first, since a `0x260F` success proves nothing).

Shipped in two commits on purpose: `7c65a9f` built it from the datasheets; `9021b32` corrected it after the
bench disproved the NAK assumption.

**Live now:** `sgp40_stdby3` (10:BD:A3:A0:96:BC), fw `v27-sgp41id`, publishing
`device_type: sgp41_gas`, `{voc_index, voc_raw, nox_index, nox_raw}`. NOx index sits at **1** = the
documented clean-air baseline.

⚠ **NOx bands are PROVISIONAL** — every other family's knots were shadow-tuned on real house data; we have
no NOx history. Re-tune after a few weeks. Recorded in ADR-0035 + `SENSOR-METHODOLOGY.md` §3.2b.

---

## What else shipped (board sweep, in Hugh's priority order)

| Task | Commit | Result |
|---|---|---|
| `containerd-unused` | `4c4c968`, `9637b52` | docker+containerd disabled (zero containers). **`docker.socket` was separately enabled** — disabling the service alone would have let socket activation restart it. |
| `healthcheck-latency-guard` | `3be03e6` | Watches the *margin*, alerts at 50% of timeout. **Found three live faults on its first run.** |
| failover leg uniformity | `d287dc1` | Removed the Midea ping, `weight -40 → -60`, and a `deploy.sh` guard. |
| `sensors-query-unbounded` | `c073de8`, `7d34cb4` | `/api/v1/sensors` **1.70s → 0.019s** on ha-2. |
| `ha2-api-bursts` | (same) | ha-api **79% → 3.9%** of a core; WAL **106MB → 0**. Same root cause. |
| `ag-unit-set-gaps` | `809b02f` | Standby unit set now **declared, not hand-curated**. |
| `os-idle-churn` | `826338d` | Idle fork/exec **6.78 → 2.67 spawns/s**. |
| `sync-firmware-tree-to-ha2` | `b076b26` | 165 files, sha256-verified. ha-2 was missing **five components**. |
| `edge-generic-images-s3-c3` | `40259f4`, `e1e28dd` | All three targets now flashable from the PWA. |

### Findings worth carrying forward

* **The household failover leg had been advertising itself unfit for ~13h** (22 priority flips/24h). Cause:
  `midea-device.env` pointed at a dead DHCP address. The guard caught it immediately.
* **Fitness probes must be differential.** Device reachability is *shared-fate* — both nodes ping the same
  appliance and get the same answer, so it cannot distinguish "this node is broken" from "the appliance is
  unplugged", and failing over cannot fix an unplugged appliance. The rule is now in
  `required-services.yaml` at the point of decision: *if this goes bad, would moving the VIP fix it?*
* **`weight` must exceed the priority gap** or health-based failover is disarmed by arithmetic
  (150−40=110 > a standby's 100). Every component "worked"; only the numbers didn't.
* **`service_healer`, not keepalived, was the fork hog** — every 30s, ~82 `systemctl` spawns.
* **A trigger, not application code**, maintains `latest_readings`: `readings` has writers in *bash*
  (`reconcile-history.sh` via the `sqlite3` CLI). The upsert guard `WHERE excluded.ts > latest_readings.ts`
  is load-bearing — backfills replay *backdated* rows.
* **ha-2 cannot BUILD firmware** (no ESP-IDF). It serves and triggers OTAs. Build on `.210` → scp → run
  `edge_ota.py` there.
* `meter_pro_c_office`'s "DEAD buffer" flag was **stale** — 2862 rows recovered. Its display clock is fine;
  its *internal history timebase* is ~41.6 days adrift and v23 re-anchors around it.

---

## Open / next

1. **Hugh: flash-update + intake the SGP41 node** into its real room. **Blocked on** deploying the SGP41
   *intake/flash surface* to ha-2 (`control.py`, `edge_discovery.py`, `app.js`, `edge_flash.py`) — wants its
   own VIP-inhibit window. The server *banding* half (`gas_compensation.py`, `viewmodel.py`) is already there.
2. **Break-glass recovery key still on `.210`** — `/etc/ha-break-glass/recovery.key.MOVE-OFFLINE.pem`, open
   since 07-10. Only Hugh can move it offline.
3. `ha-service-healer` on the air-gap standby — **deliberately excluded, an open decision, not an oversight**
   (it writes `.unfit` → changes live failover behaviour; wants its own drill).
4. `os-idle-churn`'s **ctxt/s and CAL are NOT resolved** and were higher than filed — but they are dominated
   by the interactive session itself. Re-measure on a genuinely idle box before concluding anything.
5. `ha-gas-quality-sampler` runs every 60s on **both** stacks (~3387 ctxt + 565 CAL per run). Cadence looks
   generous for a slow-moving banded value — a data-retention policy call, not a perf one.
6. Board: `d1001-graph-p2c/d/e` chain, `standby-c6-ota-intake` (LOW/deferred).
7. **Pre-existing test failures (3, unchanged all session):** `test_agents_nav` (4 components still lack
   breadcrumbs — I added `ha_gas` + `sgp41`), `test_rooms::test_empty_inputs_do_not_error`,
   `test_reconcile_merge` (environment-driven: `reconcile-history.sh` sources `instance/cluster.env`, so it
   only passes where `instance/` is absent — proven on an unmodified worktree).
