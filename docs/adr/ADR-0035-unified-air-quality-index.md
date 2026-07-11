# ADR-0035 — Unified air-quality index across heterogeneous gas sensors

**Status:** Proposed (2026-07-11) — DRAFT for Hugh's review. Design only; not yet implemented.
Supersedes the BME680-only `air_quality` metric (generalizes it; see §7).

**Directive origin:** Hugh, 2026-07-11. "We have SGP30, SGP40, and BME680 gas sensors …
they all report different data. Come up with a unified air-quality report, displayed on
the map, investigable in graphs, with its conclusions explained to the user." Scale
decision (Hugh): **5-band Good/Fair/Moderate/Poor/Very Poor with a numeric underneath.**

**Foundation:** the Phase-1 study [`docs/air-quality/SENSOR-METHODOLOGY.md`](../air-quality/SENSOR-METHODOLOGY.md)
(manufacturer semantics + real-data characterization + normalization verdict). Read it
first — this ADR turns its verdict into a concrete design. Builds on
[`server/gas_compensation.py`](../../server/gas_compensation.py).

---

## Context — three sensors, three incompatible methodologies

Per Phase 1, the three families do not report the same quantity on the same scale:

| Family | Nodes / areas | AQ signal | Basis | Vendor-sanctioned absolute mapping? |
|---|---|---|---|---|
| **SGP30** | `gas_c_bed` | TVOC (ppb, ethanol-equiv.) | **absolute** (drifts) | Yes — UBA five-level TVOC bands |
| **SGP40** | `gas_c_office`, `gas_kitchen` | VOC Index (1–500, 100=24h baseline) | **relative** (self-calibrating) | **No** — Sensirion designed it to discard concentration |
| **BME680** | `gas_hbed`, `gas_h_office` | gas resistance (Ω) | **relative** (per-node baseline) | No — raw R only; BSEC IAQ not run |

The verdict (Phase 1 §6): these **can** share one banded cleanliness scale, but only
honestly — most rooms are scored **relative to their own rolling baseline**, not against
each other in absolute units. Only SGP30 supports an absolute categorization. A design
that hides this is dishonest (Phase-1 dead-end §5.5); a design that surfaces it is
correct.

---

## Decision — one banded scale, per-family transfer functions, a basis flag

### The canonical output (per gas node, per sample)

A unified reading carries four fields:

| Field | Type | Meaning |
|---|---|---|
| `air_quality` | 0–100, **higher = cleaner** | numeric position (generalizes the existing metric; reuses its direction so nothing inverts) |
| `air_quality_band` | ordinal 1–5 → `Good` / `Fair` / `Moderate` / `Poor` / `Very Poor` | the human-facing 5-band label (the map surface) |
| `air_quality_basis` | `absolute` \| `relative` | **the honesty flag** — is this comparable room-to-room (absolute) or only vs this room's own recent baseline (relative)? |
| `air_quality_conf` | `ok` \| `warmup` \| `burn_in` \| `stale` \| `no_ref` | confidence / quality gate (drives greying-out + the explanation) |

Bands map to numeric ranges monotonically:

| Band | Label | `air_quality` range |
|---|---|---|
| 5 | Good | 80–100 |
| 4 | Fair | 60–80 |
| 3 | Moderate | 40–60 |
| 2 | Poor | 20–40 |
| 1 | Very Poor | 0–20 |

> Bands are the primary artifact (what the map shows, what gets explained); the 0–100 is
> the fine-grained position within/under the bands (what graphs plot). Two rooms may both
> read "Good" but only compare *numerically* when both are `basis=absolute`.

### Per-family transfer functions

Each family maps its raw signal → `(band, air_quality, basis, conf)`. Thresholds below are
**proposed starting points to shadow-tune on our captured distributions** (Phase-1 §4);
they are not claimed final.

**SGP30 → ABSOLUTE (UBA-anchored).** TVOC ppb through the UBA five-level bands
(Phase-1 §3.4), which *are* the 5 bands. `air_quality` interpolated **logarithmically**
within a band (UBA is a log scale). `basis = absolute`.

| TVOC ppb | UBA level | Band | Notes |
|---|---|---|---|
| 0 – 65 | 1 | Good | target |
| 65 – 220 | 2 | Fair | |
| 220 – 660 | 3 | Moderate | ventilate |
| 660 – 2200 | 4 | Poor | |
| > 2200 | 5 | Very Poor | |

- Warmup gate: `eco2==400 AND tvoc==0` → `conf=warmup`, no band (Phase-1 §4.1, ~1% of rows).
- eCO2 is **not** used for banding (it's a proxy-of-a-proxy, Phase-1 §3.1) — carried as a
  secondary display metric only.
- Drift caveat (Phase-1 §4.1): SGP30's absolute baseline creeps over days. v1 bands on raw
  ppb and *labels the reading absolute with a drift note*; an optional slow drift-correction
  is deferred (open question Q3).

**SGP40 → RELATIVE (native Sensirion scale).** The firmware already emits the VOC Index
via Sensirion's Gas Index Algorithm (relative to the sensor's own **24 h** baseline,
100=baseline). We do **not** re-baseline — we band the received index directly on its
native meaning. `basis = relative`.

| VOC Index | Band | Meaning (vs room's 24 h normal) |
|---|---|---|
| ≤ 100 | Good | at/below recent baseline |
| 100 – 200 | Fair | mildly elevated |
| 200 – 300 | Moderate | elevated |
| 300 – 400 | Poor | high |
| > 400 | Very Poor | extreme |

- `air_quality` = linear map of index into the band's 0–100 range (inverted: higher index
  → lower air_quality).
- Startup gate: first ~45 s / `voc_index==0` sentinels → `conf=warmup` (Phase-1 §4.2).

**BME680 → RELATIVE (de-clamped resistance ratio).** Reuse `gas_compensation.py`'s
humidity-compensated, rolling-baseline approach — but **fix the ceiling clamp** that
pinned ~22 % of samples at 100 (Phase-1 dead-end §5.1). Band on the resistance ratio
`r = gas_ohm_humcomp / baseline` (baseline = rolling p95 clean-air R over 24–48 h;
`clean_air_baseline()`), since pollution = resistance *drop*. `basis = relative`.

| r = gas_ohm/baseline | Band |
|---|---|
| ≥ 0.90 | Good |
| 0.70 – 0.90 | Fair |
| 0.50 – 0.70 | Moderate |
| 0.30 – 0.50 | Poor |
| < 0.30 | Very Poor |

- Humidity compensation stays (uses the co-located `ambient_ref`'s TRUE RH, not the
  BME680's self-heated value — the whole reason `gas_compensation.py` exists). It corrects
  `gas_ohm` *before* the ratio rather than adding a separate 25 % term, so the band reflects
  actual VOC load, not humidity artifacts.
- Gates: `gas_valid==0` → drop; no `ambient_ref` resolved → `conf=no_ref`; baseline still
  forming (< burn-in) → `conf=burn_in`.
- **Freshness gate (already shipped, ADR-precedent):** if raw `gas_ohm` is > 600 s old the
  derived reading is not produced (`conf=stale`) — the fix from the 2026-07-11 frozen-derived
  incident, now generalized as a first-class confidence state.

### Rolling windows (why each is what it is)

| Family | Window | Rationale |
|---|---|---|
| SGP30 | none (absolute) | UBA bands are fixed thresholds; drift-correction deferred (Q3) |
| SGP40 | 24 h (in firmware) | Sensirion's Gas Index Algorithm already self-baselines over 24 h — we inherit it, don't duplicate it |
| BME680 | 24–48 h clean-air p95 | matches BSEC's multi-day auto-calibration shape; tracks slow MOX drift (`clean_air_baseline`) |

### Per-area aggregation (the map surface)

Each area currently has ≤ 1 gas node → the area's unified reading is that node's reading.
**If an area ever has multiple gas nodes:** report the **worst (most conservative) band**,
prefer an `absolute`-basis reading to break ties, and expose both underneath. Never average
across basis types (an absolute band and a relative band are not the same statement).

---

## Storage & catalog (compute-and-store, per data-storage-is-primary)

- **Generalize** the derived `air_quality` writer (`gas_quality_persist.py`) from BME680-only
  to all three families; keep the freshness-gate. Persist `air_quality`, `air_quality_band`,
  `air_quality_basis`, `air_quality_conf` as stored series (idempotent `INSERT OR IGNORE`).
- Add `air_quality_band` (+ basis/conf) to `METRIC_CATALOG` and `NORMAL_RANGES`
  (`server/api/viewmodel.py`). `air_quality` stays but its computation changes (de-clamp +
  cross-family) → **recompute/backfill** existing BME680 `air_quality` history (`--backfill`)
  so the graph is consistent with the new definition. Old values are not silently mixed with
  new ones (open question Q4: version-tag or full recompute).

---

## Explanation-layer contract (Phase 6 hook)

The four fields are exactly what a per-reading explanation needs. Contract for the generator:

> "**{band}** ({air_quality}/100). From {family}'s {raw_signal}={value} — {absolute:
> ‘UBA level {n}, comparable across rooms’ | relative: ‘{x}% {above/below} this room's
> recent baseline; a relative measure, not comparable to other rooms’}. {conf note if not ok}."

Example: *"Fair (68/100). From SGP40 VOC Index 140 — 40% above this room's recent baseline;
a relative measure, not comparable to other rooms."*

---

## Consequences

- **Honest by construction.** The `basis` flag makes the absolute/relative distinction a
  first-class part of every reading, map badge, and explanation — the thing Phase 1 said we
  must not hide.
- **One scale, three lineages.** The map shows one banded report everywhere; drill-down
  (Phase 5) reveals the family-specific raw signal that produced it.
- **De-clamping changes BME680 history** — a backfill/recompute is required; the old ceiling
  at 100 goes away, so historical graphs shift (Q4).
- **Relative bands can mislead if read as absolute.** Mitigated by always showing the basis;
  a room reading "Good" on a relative sensor means "clean vs its own normal," which a very
  polluted-but-stable room could also show. Documented in the explanation copy.
- Reuses existing fusion + reference-resolution + freshness-gate; no firmware change needed
  (all three families already publish what we need).

## Open questions (for review)

- **Q1** — Band the SGP40 on its **native index** (proposed) vs on a **rolling percentile of
  its own recent distribution**? Native is simpler and uses Sensirion's meaning; percentile
  is more adaptive but self-referential ("Good" ~half the time). Proposed: native.
- **Q2** — Are the proposed thresholds right for our units? They need a **shadow-tune pass**
  against the Phase-1 captured distributions before acceptance (esp. BME680 r-ratio bands and
  SGP40 cutoffs, given observed median VOC ~78).
- **Q3** — SGP30 slow drift-correction: worth it, or is the absolute label + drift note
  enough for v1? Proposed: defer.
- **Q4** — BME680 `air_quality` recompute: full historical backfill vs version-tag the metric
  so old/new aren't mixed? Proposed: backfill with a note.
- **Q5** — Metric naming: generalize `air_quality` (proposed) vs introduce a new `aqi_*`
  family and leave `air_quality` as the BME680 legacy? Proposed: generalize.

## Rollout (mirrors prior ADRs — design → shadow-tune → implement → deploy)

1. **This ADR accepted** (Hugh review of the 5 open questions).
2. **Shadow-tune thresholds** on the Phase-1 captured data (Q2) — pure-function tuning, no writes.
3. **Implement** (Phase 3): generalize `gas_compensation` + `gas_quality_persist`; catalog + ranges.
4. **Map surface** (Phase 4) → **graph drill-down** (Phase 5) → **explanation layer** (Phase 6).
5. **Deploy to ha-2** via the scp + checksum-verify + tripwire discipline
   ([[airgap-checkout-drift]]); backfill on ha-2.
