# Rollup ladder + panel replica sync — dev ⇄ ops coordination contract

**Date:** 2026-07-02  **Status:** Working contract (living). Implements **ADR-0022** (multi-resolution rollup
ladder) + **ADR-0019** (panel data agent). **Owners:** schema = shared; server engine + query selector +
serve endpoints = **dev**; panel `ha_replica` (pull + local query) = **ops**. Pairs with board items
`rollup-ladder-server` (dev) and `ha-replica-panel` (ops).

> **Why this doc.** ops is blocked on ha-replica-panel pending dev's rung schema + sync contract. This pins
> the interface both sides build to in parallel. ADR-0022 already fixed the ladder + retention + row shape,
> so this is mechanism, not re-deciding. Reply on `rollup-ladder-server` to counter any of it.

## 0. The shape of it
Raw readings (hot.db + parquet) stay server-only. The **rungs** — compact sqlite, MB not GB — are the
canonical long-history representation and the **only thing the panel replicates**. Server builds them; panel
pulls a file-level copy to SD and queries it locally (offline, instant charts).

```
raw readings (parquet, server)  ──rollup engine──▶  rungs.db (sqlite, server)
                                                          │  HTTP pull (OTA-style)
                                                          ▼
                                                    /sdcard/rungs.db (panel, ha_replica)
```

## 1. THE SCHEMA — rung table (shared; matches the P4-validated shape)
One table, all resolutions. INTEGER epoch buckets, `WITHOUT ROWID` (covering btree). Validated on P4 at
74 B/row, 518 k rows = 8.9 ms warm range query (`firmware/components/sqlite3/README.md`).

```sql
CREATE TABLE rung (
  res          TEXT    NOT NULL,   -- '1min' | '1hour' | '1day' | '1week'
  device_id    TEXT    NOT NULL,
  metric       TEXT    NOT NULL,
  bucket_start INTEGER NOT NULL,   -- epoch seconds UTC, floored to res
  vmin  REAL, vmax REAL, vmean REAL, vcount INTEGER, vlast REAL,
  PRIMARY KEY (res, device_id, metric, bucket_start)
) WITHOUT ROWID;
```

**⚠ ops coordination point — PK column order.** ADR-0022/the P4 spike used
`PRIMARY KEY(res, bucket_start, device_id, metric)`. I propose swapping to **`(res, device_id, metric,
bucket_start)`** so one series' buckets are *contiguous* — the actual chart query is "one device_id + one
metric + a time range at one res", which becomes a single covering range scan (vs. scanning every device per
bucket). Same row size, same 74 B/row. **ops: can you re-validate the P4 query timing with this PK order?**
If it regresses, we keep the spike's order. This is the one shape decision to settle before either side
freezes code.

- **Aggregates:** `vmin`/`vmax` mandatory (envelope + spikes). `vmean` weighted. `vcount` enables composition.
  `vlast` = last raw value in the bucket. (Median lives only in the existing daily `summaries`, not composable.)
- **bucket_start** floors to the rung: 1min→`ts - ts%60`, 1hour→`%3600`, 1day→UTC midnight, 1week→UTC Mon 00:00.

## 2. SERVER ROLLUP ENGINE — dev (`rollup-ladder-server`)
Incremental, idempotent, cutoff-driven — mirrors the compactor pattern (`server/storage/compactor.py`).

- **1min from raw:** read `readings` (hot.db) since a per-res high-water mark; group by
  `(device_id, metric, floor(ts,60))`; upsert `min/max/mean(weighted)/count/last`. Composes raw parquet too
  for backfill of already-archived days.
- **Cascade:** `1hour ← 60×1min`, `1day ← 24×1hour`, `1week ← 7×1day` — `vmin=min(vmin)`, `vmax=max(vmax)`,
  `vcount=Σvcount`, `vmean=Σ(vmean·vcount)/Σvcount`, `vlast=last`. Only recompute *complete* dest buckets
  (a dest bucket is finalized once all source buckets in it exist).
- **Idempotency:** `INSERT … ON CONFLICT(res,device_id,metric,bucket_start) DO UPDATE` — a re-run is a no-op /
  self-heals a partial bucket. Same discipline as the hot-tier reconcile (`INSERT OR IGNORE` on the unique key).
- **Retention (tuning knobs):** prune `1min` >**7 d** (tuned DOWN from the ADR-0022 90 d default), `1hour`
  >2 y; `1day`/`1week` kept forever. Raw stays as parquet (forensic) — not pruned by this engine.
  *Why 7 d:* sampling here is ~1/min, so the `1min` rung barely compacts vs raw — 90 d of it made the
  panel-replica `rungs.db` ~700 MB (vs the "MB-scale" the ladder promises). A wall panel needs `1min` only
  for recent ≤2 d zoom; older spans use `1hour`. Bump it back up if fine-grained deep history is wanted, at
  a replica-size cost (~65 k `1min` rows/day here → ~7 MB/day).
- **Cadence:** `ha-rollup.timer` ~every 5 min (1min rung fresh enough for a wall panel; cascades fire as
  buckets complete). ⚠ **installing the new systemd unit on .210 is a Hugh-gated deploy** (new unit) — engine
  code + tests are ungated; I'll hand the unit for install.
- **Output:** `instance/db/rungs.db` (single file, all resolutions). Engine module `server/storage/rollup.py`
  + `server/tests/test_rollup.py` (composition math + idempotency + retention).

## 3. QUERY RESOLUTION-SELECTOR — dev (server + mirrored on panel)   ✅ BUILT (`rollup.select_resolution`)
`GET /devices/{id}/readings?…&res=auto` picks the rung by span so charts stay legible (opt-in: omit `res`
or `res=raw` = exact raw behaviour, unchanged for existing clients; or force `res=1min|1hour|1day|1week`).

| Span | Rung (`select_resolution`) | ~pts |
|---|---|---|
| ≤ 2 h | **raw** (server has raw) / **1min** (panel — no raw) | ≤240 |
| ≤ 2 d | 1min | ≤2880 |
| ≤ 2 mo | 1hour | ≤1440 |
| ≤ 4 y | 1day | ≤1460 |
| longer | 1week | — |

`select_resolution(span_s)` in `server/storage/rollup.py` is the **single source of truth** — a pure span→rung
fn (no recency arg, so it's trivial to mirror). The server returns `raw` for ≤2h (it has the raw tier); the
panel substitutes `1min`. Rung responses carry `value=vmean` + `min`/`max`/`count` per bucket → band + line;
calibration offsets are applied to `value`/`min`/`max`.

**Selector × retention (important for the panel mirror):** the selector is span-only, so a 2-day window from
*months ago* selects `1min` — but `1min` is pruned to 7 d (§ retention). The **server** handles this safely:
under `res=auto`, an empty rung result falls through to raw/parquet (forever). The **panel** has no raw, so
ops's mirror should **escalate to the next coarser rung when the local rung query returns no rows** for the
window (1min→1hour→1day). A *forced* `res=1min` is returned as-is (may be empty) — caller's choice.

## 4. SYNC CONTRACT — dev serves, ops pulls (HTTP, OTA-style)
fs_ops-over-MQTT (1536 B/read) is too slow for an MB file. Reuse the **OTA transport**: server serves files
over HTTP; panel pulls with `esp_http_client` (already in the panel for OTA). LAN-open like today's reads
(panel is unsigned-LAN; tightens under `tls-r9-auth` when off-LAN).

**Endpoints (dev builds):**
- `GET /api/v1/rung/manifest.json` → per-res `{latest_bucket_start, rows, sha256}` + `updated_ts`. Panel polls
  this (tiny) to know if it's behind without pulling the DB. Mirrors the parquet `manifest.json` pattern.
- `GET /api/v1/rung/full.db` → the whole `rungs.db` (streamed). **Seed / cold-start** path: panel copies it to
  SD directly (fast — no row-by-row insert; the P4 spike showed 518 k-row insert is 124 s, a file copy is not).
- `GET /api/v1/rung/since?res={res}&after={bucket_start}` → **NDJSON**, one row per line
  `{"res","device_id","metric","bucket_start","vmin","vmax","vmean","vcount","vlast"}`. ✅ BUILT.
  **`after` is INCLUSIVE (`bucket_start >= after`)** — the newest bucket is recompute-and-replaced until it
  closes, so it must be re-sent; the consumer upserts by `(res,device_id,metric,bucket_start)`, making the
  one-row overlap a no-op. **Steady-state** path: panel INSERTs the small delta and advances its HWM.
- `GET /api/v1/replica/config` → the opaque encrypted config blob (ADR-0022) — same transport, panel stores it.

**Panel `ha_replica` loop (ops builds):** on boot → if no local `rungs.db`, pull `full.db` to SD; else poll
`manifest.json` each cycle; for any rung behind, pull `since?res&after=<local_hwm>` and INSERT; advance HWM.
Presence-gated on the SD card (ADR-0019). Charts read the **local** rung DB via the sqlite3 VFS — instant,
offline, no server round-trip on tap.

**Incremental semantics** reuse the reconcile-history idea (windowed `WHERE bucket_start > hwm`), keyed
idempotently so an overlapping re-pull is a no-op.

## 5. Division of labor (dev confirms/counters on `rollup-ladder-server`)
| Work | Owner | Board item |
|---|---|---|
| Rung schema (this §1) | **shared** | this doc |
| Rollup engine `server/storage/rollup.py` + retention + tests | **dev** | `rollup-ladder-server` |
| Query resolution-selector on `/readings` + publish the selector fn | **dev** | `rollup-ladder-server` |
| Rung HTTP serve endpoints + `manifest.json` | **dev** | `rollup-ladder-server` |
| `ha-rollup.timer` unit (install = **Hugh-gated**) | **dev** builds / **Hugh** installs | `rollup-ladder-server` |
| Panel `ha_replica`: HTTP pull → SD → local rung query + selector | **ops** | `ha-replica-panel` |

## 6. Phasing (each phase independently shippable)
- **Phase 1 (unblocks ops):** schema frozen + engine builds `rungs.db` + `full.db` serve + panel seed-pull &
  local query. Proves end-to-end on real data. ops can build the whole panel consumer against `full.db` now.
- **Phase 2:** `manifest.json` + `since` incremental (steady-state efficiency).
- **Phase 3:** config blob + parquet-recovery pull (panel as a recovery tier, ADR-0016/0018).

## 7. Open questions
- **PK column order** (§1) — the one thing needing an ops re-validate before code freeze. Everything else in
  ADR-0022 is pre-decided; no Hugh decision is blocking. Retention constants are the ADR defaults (tunable later).

---

# Part C — Canonical node state-change envelope + coordinator consumer
**This answers Hugh's open event-driven-reconcile question** (board `reterminal-panel`). Different subsystem
from §1–7 (this is mesh **relay** coordination, ADR-0015/0023 — not data replication), bundled here per the
co-design so ops has one draft. Extends ADR-0023 (the census): that gave the coordinator a **server→node**
push (`reach/req`, timer-driven); this is the **node→server** inverse.

## C.1 Producer — ALREADY LIVE on the panel (ops, verified 2026-07-02)
The D1001 emits, on each power edge, a **retained `d1001-beachhead/power {on_wall, ble_relay}`** + an immediate
`ha_reach_report`. Verified: unplug → `on_wall:false` + BLE pause; replug → reverse. **The producer is done** —
we just canonicalize the shape and I build the consumer.

## C.2 The canonical envelope (shared — ops aligns the panel to this)
One topic all nodes use for known-state-change signals, in the edge namespace the coordinator already owns
(`home/edge/+/reach`, `…/adv`):

```jsonc
// home/edge/<node>/event   (RETAINED — current-state semantics; a reconnecting coordinator re-reads it)
{
  "schema": 1,
  "node":   "d1001-beachhead",
  "kind":   "power",                 // extensible: power | link | thermal | …
  "ts":     "2026-07-02T19:40:00Z",
  "state":  { "on_wall": false, "relaying": false, "censusing": false }
}
```
- `kind:power` `state` fields: `on_wall` (wall vs battery), `relaying` (BLE relay active), `censusing` (reach
  census active). ops's existing `{on_wall, ble_relay}` maps 1:1 (`ble_relay`→`relaying`); I'll consume this shape.

## C.3 Consumer — dev (the missing half, extends `coordinator.py`)
- Subscribe `home/edge/+/event`.
- **`relaying` → false** (node dropped off, e.g. panel on battery): (1) push `reach/req` to the *other* enrolled
  nodes for fresh reach, (2) run a **targeted reconcile** treating this node as reach-less → its covered meters
  reassign to the next-best hearer. **Prompt — short/zero dwell** (reuse the existing "drop-to-empty publishes
  immediately" path); an orphaned meter must not wait 900 s.
- **`relaying` → true** (returned): targeted reconcile to re-include it under the **full 900 s dwell** (anti-flap).
  The node re-censuses immediately (producer already does), so its reach is fresh for the pass.
- **Coalesce/debounce:** collapse events within ~5 s into one pass; per-node rate-limit to absorb power flapping.
  Retained events re-fire on coordinator restart → one idempotent reconcile on startup (fine).
- **Auth:** the event only *prompts* a recompute from already-verified reach — it changes no assignment itself —
  so an unsigned LAN event (the panel is unsigned-LAN) is low-risk. Signs later under `tls-r9-auth`.

## C.4 Division
| Work | Owner | Board item |
|---|---|---|
| Canonical `event` envelope (§C.2) | **shared** | this doc |
| Coordinator event consumer: subscribe + targeted reconcile + asymmetric dwell + coalesce + tests | **dev** | new `event-reconcile` |
| Panel producer alignment (`d1001-beachhead/power` → `home/edge/d1001-beachhead/event`) | **ops** | `reterminal-panel` |
