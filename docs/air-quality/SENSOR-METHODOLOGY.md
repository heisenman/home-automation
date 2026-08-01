# Gas-Sensor Data Methodology & Air-Quality Normalization Study

**Status:** Phase 1 deliverable — investigation & record (design not yet implemented)
**Date:** 2026-07-11
**Author:** dev2 (Claude) with Hugh
**Scope:** SGP30, SGP40, BME680 — how each reports air-quality data, whether their
outputs can be legitimately normalized onto ONE directly-comparable scale, and the
recommended fusion strategy.

> **Read this first if you are adding a NEW gas sensor.** This document is written to
> be a reusable template. The conclusions matter, but so does the *path* — especially
> the [dead ends](#5-dead-ends--rejected-approaches-and-why). Jump to
> [§7 "Adding a new gas sensor" checklist](#7-adding-a-new-gas-sensor-a-repeatable-checklist).

---

## 1. Purpose & the core problem

We run three different air-quality gas sensor families. They do **not** report the same
quantity, on the same scale, with the same meaning:

| Family | Nodes / areas | AQ-relevant signal | Scale nature |
|---|---|---|---|
| **SGP30** | `gas_c_bed` (c_bed) | TVOC (ppb), eCO2 (ppm) | **absolute-ish, drifts** |
| **SGP40** | `gas_c_office` (c_office), `gas_kitchen` (kitchen) | VOC Index (1–500, 100=baseline) | **explicitly relative, self-calibrating** |
| **BME680** | `gas_hbed` (h_bed), `gas_h_office` (h_office), `gas_standby` (spare) | gas resistance (Ω) → derived `air_quality` (0–100) | **relative to rolling baseline** |

The goal is a single **unified, banded air-quality report** (Good / Fair / Moderate /
Poor / Very Poor, with a numeric index underneath) shown on the map, drillable in
graphs, and explainable to a human. The hard part is **not** display — it is that
"VOC Index 100", "TVOC 220 ppb", and "gas resistance 50 kΩ" are three physically
different statements. A credible unified index must normalize them honestly and
declare where the honesty runs out.

---

## 2. Methodology — how we investigated (reproducible)

Two parallel workstreams fed one verdict. **Right-size the effort** (see the meta dead
end in §5): this is documented-spec research, not contested-claim research.

### Workstream A — Manufacturer semantics (what the numbers officially mean)
- **Tool:** targeted `WebSearch` against primary domains (`sensirion.com`,
  `bosch-sensortec.com`, Sensirion/Bosch GitHub). ~3 searches.
- **Questions:** for each sensor — what does the output physically represent, what is
  its reference basis, does the vendor sanction any absolute-concentration mapping, and
  what is the baseline/adaptation behavior.
- Sources are listed in §8.

### Workstream B — Real-data characterization (how OUR units actually behave)
- **Data stores (on THIS box, .210/dev):**
  - Hot DB: `instance/db/hot.db` — table `readings(id,ts,device_id,device_type,area,transport,metric,value,unit,schema_v,authoritative)`; holds only ts ≥ yesterday 00:00 UTC.
  - Parquet archive: `instance/db/parquet/year=2026/month=MM/2026-MM.parquet` — same columns, everything older than yesterday. **Most gas history lives here.**
  - Rollup ladder: `instance/db/rungs.db` — 1min/1h/1d/1w min/max/mean/count/last.
  - Writer: `server/storage/writer.py`; compaction: `server/storage/compactor.py`; rollups: `server/storage/rollup.py`. Derived `air_quality` written by `server/maintenance/gas_quality_persist.py`, formula in `server/gas_compensation.py`.
- **Query path:** `duckdb` (in `venv/`) reading the Parquet glob `UNION ALL` hot.db via `ATTACH ... (TYPE SQLITE, READ_ONLY)`, deduped on `(device_id,ts,metric)`. Idempotency key is `(device_id, ts, metric)`; `ts` is ISO-8601 UTC text, lexically sortable. Reproducible view:

```sql
ATTACH 'instance/db/hot.db' AS hot (TYPE SQLITE, READ_ONLY);
CREATE VIEW allr AS
SELECT ts,device_id,metric,value FROM (
  SELECT ts,device_id,metric,value FROM read_parquet('instance/db/parquet/year=2026/month=*/*.parquet')
  UNION ALL SELECT ts,device_id,metric,value FROM hot.readings)
QUALIFY row_number() OVER (PARTITION BY device_id,ts,metric ORDER BY value)=1;
```

- **Caveat — short history:** every gas node is NEW (first readings 2026-07-02…07-08).
  No pre-July gas history exists. Long-term-drift conclusions are directional, not
  seasonal.

---

## 3. Findings — manufacturer semantics

### 3.1 SGP30 — eCO2 (ppm) + TVOC (ppb)
- **TVOC is an *ethanol-equivalent* concentration.** The sensor is a single MOX element;
  it cannot speciate. Sensirion calibrates its TVOC output against an ethanol reference,
  so "220 ppb" means "as reducing as 220 ppb of ethanol would be", not 220 ppb of any
  specific compound. It is an **absolute-ish** number but only meaningful as a proxy.
- **eCO2 is NOT a CO2 measurement.** There is no NDIR/CO2 path on an SGP30. eCO2 is
  *computed* from the H2/reducing-gas signal on the assumption that indoor VOC/CO2 rise
  together (human occupancy). It is a derived proxy of a proxy — treat as weak.
- **On-chip baseline compensation, and it drifts.** The SGP30 runs an internal dynamic
  baseline that takes **~12 h** to settle from cold and is meant to be persisted
  (get/set_baseline) across reboots. Absolute readings **creep** over days as the
  baseline adapts — confirmed strongly in our own data (§4.1).
- **Absolute anchor available:** TVOC ppb can be mapped to the German UBA five-level
  indoor-air scale (§3.4). This is the ONLY one of our three sensors with a defensible
  absolute categorization.

### 3.2 SGP40 — VOC Index (1–500) via Sensirion Gas Index Algorithm
- **Explicitly a RELATIVE, self-calibrating index — not a concentration.** The raw
  SRAW_VOC tick is fed to Sensirion's Gas Index Algorithm, which does
  *statistical gain-offset normalization*, estimating sensor baseline and sensitivity
  from **the past 24 h** of statistics and applying an exponentially-decaying update.
- **Scale meaning:** 100 = the sensor's own recent (24 h) baseline. Higher = more VOCs
  than that room's recent normal; lower = cleaner than recent normal. Range 1–500.
- **No sanctioned concentration mapping.** Sensirion designed it precisely to be
  sensor-to-sensor invariant and to detect *changes*, deliberately discarding absolute
  concentration. There is no vendor-blessed VOC-Index→ppb function. Any such mapping we
  invent would be fiction.
- **Consequence:** two SGP40s in two rooms both reading "100" are each at *their own*
  baseline — the rooms are NOT necessarily equally clean in absolute terms.

### 3.2b SGP41 — VOC Index + NOx Index (added 2026-08-01)
- **VOC pixel is the SGP40's**, same algorithm, same semantics as §3.2 above. Everything
  said there applies unchanged.
- **NOx Index does NOT share the VOC scale.** This is the trap. The VOC Index centers on
  **100** (= own 24 h baseline); the NOx Index sits at **1** in clean air and only climbs
  on a NOx event. Range is 1–500 for both, but "100" means *ordinary* on one pixel and
  *a serious event* on the other. Copying the VOC knots to NOx would band a bad reading
  as fine. (`GasIndexAlgorithm_NOX_INDEX_OFFSET_DEFAULT = 1.f` vs `VOC…= 100.f`.)
- **What NOx actually indicates:** combustion — a gas hob, wood stove, or traffic drawn
  in from outside. It is a genuinely *different* pollutant axis, not a better VOC.
- **NOx learns far more slowly:** `INIT_DURATION_MEAN_NOX` = 4.75 h vs 0.75 h for VOC,
  plus a mandatory 10 s hotplate conditioning phase at boot. A fresh node legitimately
  has no NOx reading for hours — that must read as "not yet valid", never as "pristine".
- **Fusion rule chosen: worst-of, not average.** The band reports the lower of the two
  pixel scores and names which one drove it (`nox_dominant`). Averaging would let clean
  VOC air mask a live combustion event — the exact thing the second pixel exists to catch.
  This mirrors the dominant-pollutant rule in a regulatory AQI.
- ⚠ **Banding is PROVISIONAL — step 3 of the §7 checklist is NOT done.** The SGP30/SGP40/
  BME680 knots came out of real characterization against weeks of this house's data. We
  have **zero** NOx history, so the NOx knots (`_NOX_X/_NOX_Y` in `server/gas_compensation.py`)
  are seeded from Sensirion's documented scale alone. Re-tune once a real SGP41 has banked
  a few weeks, and record the result here.

### 3.3 BME680 — gas resistance (Ω), and Bosch BSEC IAQ (which we do NOT run)
- **Raw gas resistance direction:** the MOX gas layer's resistance **RISES in clean air
  and FALLS as reducing-VOC concentration rises.** So "higher Ω = cleaner." It is
  strongly **temperature- and humidity-dependent** (the heater plate and ambient water
  vapor both shift the baseline), which is why compensation is mandatory.
- **Device-specific absolute resistance.** Every MOX element has its own absolute R.
  Our two nodes differ ~1.8× for the *same* clean air (§4.3). Raw Ω therefore can only
  ever be normalized **per-node/relative**, never cross-compared as an absolute number.
- **BSEC IAQ (0–500):** Bosch's closed-source BSEC library turns raw R into an IAQ index
  (0 = clean … 500 = heavily polluted; IAQ 25 ≈ typical good air, 250 ≈ typical
  polluted). It auto-calibrates over **~4 days** of history and needs a burn-in. **We do
  not run BSEC** (closed blob; our firmware computes raw Ω from Bosch datasheet
  polynomials only).
- **Defensible index WITHOUT BSEC exists and is the accepted path.** The well-known
  `raspi-bme680-iaq` approach (and our own `gas_compensation.py`) derives a
  humidity-compensated 0–100 cleanliness score from raw resistance against a rolling
  clean-air baseline. This is the same *shape* as BSEC's self-calibration and is what we
  should generalize.

### 3.4 The absolute anchor: German UBA TVOC five-level scale
Applies directly only to SGP30 TVOC (ppb → mg/m³ via ~4.9 µg/m³ per ppb):

| Level | TVOC (mg/m³) | ≈ ppb | Hygienic assessment |
|---|---|---|---|
| 1 | 0 – 0.3 | 0 – ~65 | No concern (target) |
| 2 | 0.3 – 1 | ~65 – ~220 | No significant concern |
| 3 | 1 – 3 | ~220 – ~660 | Some concern (ventilate) |
| 4 | 3 – 10 | ~660 – ~2200 | Major concern |
| 5 | 10 – 25 | ~2200 – ~5500 | Unacceptable |

(UBA precautionary guidance: avoid sustained TVOC > 950 µg/m³ ≈ 195 ppb.)

**These five levels are the natural anchor for our 5-band unified scale** (Good / Fair /
Moderate / Poor / Very Poor), and give the banding real-world meaning for the one sensor
that can support absolute categorization.

---

## 4. Findings — real-data characterization (our units)

Full percentile tables and cadence in the Phase-1 characterization run; highlights:

### 4.1 SGP30 `gas_c_bed` — drift confirmed
- TVOC ppb: median 109, p95 367, max 527 (right-skewed). eCO2 ppm: median 570, p95 1433.
- **Strong non-stationary baseline creep:** daily-mean TVOC ran 43 → 292 → 124 ppb over
  6 days; eCO2 daily-min lifted off the 400 floor after 07-06. This is textbook SGP30
  baseline drift — a *fixed* ppb threshold would mis-band the same air on different days.
- Warmup/floor artifacts: `eco2==400` clamp = **13.7%** of samples; both-floor
  (eco2==400 AND tvoc==0) = 525 samples (just-reset baseline). **Exclude both-floor;
  treat standalone eco2==400 as left-censored.**

### 4.2 SGP40 `gas_c_office`, `gas_kitchen` — "100" is nominal, not observed
- Medians **~78** (below the nominal 100). Only ~13% of samples in [90,110]; ~61% in
  [50,150]. p95 ≈ 237/265, max 465/496 (no ceiling clamping).
- **Both rooms rise and fall together day-to-day** (shared house-air effect, not
  per-sensor). Confirms the index tracks *changes*, and confirms the 24 h self-baseline:
  our observed center sits where the algorithm's recent window put it, not at a fixed 100.
- `voc_raw` ~30k–33k, tight (sd ~1.5%); a handful of value==2 sentinels to filter.

### 4.3 BME680 `gas_hbed`, `gas_h_office` — device-specific R, ceiling-clamped derived index
- Raw gas_ohm is **per-node**: h_bed median ~42.6 kΩ vs h_office median ~76.9 kΩ
  (~1.8×) for equivalently clean air → **relative-only normalization is mandatory.**
- Existing derived `air_quality` (0–100) is **ceiling-clamped**: ~22% of h_office samples
  pinned at ~100 (poor dynamic range) because the gas term saturates at
  `min(gas_ohm/baseline,1)`. **Normalize the underlying gas_ohm; do not reuse the
  saturated 0–100 as the fusion input.**
- `gas_valid` ≈ always 1 (drop the ~7–8 invalid samples/node). Zero-value rows in
  T/RH/pressure (~0.7–1.7%) must be filtered (`value>0`) or they poison means.

### 4.4 ⚠ Operational data-integrity flag (not a design issue — a live bug)
Both BME680 nodes **stopped emitting raw telemetry ~2026-07-10 00:55–01:51** (gas_ohm,
T, RH, pressure all flatline), yet derived `air_quality` **kept being written through
07-11** from the frozen last gas_ohm. The derived series looks alive; its input is dead.
**Post-07-10 `air_quality` on h_bed/h_office is not trustworthy.** Logged separately for
a raw-ingest diagnostic; do not let the unified index inherit this staleness (see §6
freshness gating).

---

## 5. Dead ends & rejected approaches (and WHY)

> The most reusable part of this document. Each was considered and rejected with evidence.

1. **Reuse the existing BME680 `air_quality` (0–100) as the unified index, as-is.**
   ❌ Rejected — it is **ceiling-clamped** (§4.3): ~22% of samples pinned at 100, almost
   no dynamic range in the "clean" region. Fine as a rough gauge, useless as the fusion
   backbone. *Do* reuse its *shape* (humidity-compensated, rolling clean-air baseline);
   do *not* reuse its saturated output. Normalize raw gas_ohm instead.

2. **Assume SGP40 VOC Index centers on 100, band around that.**
   ❌ Rejected — observed medians are **~78**, only ~13% of samples near 100 (§4.2). The
   "100" is a nominal reference the 24 h algorithm drifts around, not an observed center.
   Band against the sensor's *own rolling distribution*, not the literal number 100.

3. **Map SGP40 VOC Index → absolute TVOC ppb so all three share absolute units.**
   ❌ Rejected — Sensirion explicitly designed the Gas Index Algorithm to *discard*
   absolute concentration (§3.2); there is no sanctioned inverse. Any mapping we wrote
   would be fabricated precision. SGP40 can only contribute a *relative* band.

4. **Use fixed absolute thresholds for everything (one ppb/Ω table for all rooms/days).**
   ❌ Rejected — every series is **non-stationary** (§4.1, §4.2): SGP30 baseline creeps
   over days, SGP40/BME680 are relative by construction. A fixed threshold mis-bands the
   same air as conditions drift. The correct shape is a **rolling/adaptive baseline**.

5. **Force a single absolute cross-room scale so "82 in room A" == "82 in room B".**
   ❌ Rejected as dishonest for 2 of 3 families. Only SGP30 has an absolute anchor
   (§3.4). SGP40 and BME680 are relative-to-own-baseline; equal numbers do NOT mean equal
   absolute air. The unified scale must **flag each reading's basis** (absolute vs
   relative) rather than pretend uniformity — this is the honesty requirement.

6. **Trust the BME680 derived `air_quality` freshness.**
   ❌ Rejected — raw ingest died 07-10 while the derived metric kept writing (§4.4). Any
   unified index must **gate on raw-input freshness**, not just on "was a value written".

7. **(META) Use the full deep-research harness for the manufacturer semantics.**
   ❌ Rejected mid-flight — the harness fans out to ~99 agents with 3-vote adversarial
   verification per claim (projected ~3M tokens). That is the right tool for *contested*
   claims, but datasheet semantics are *documented spec*: ~3 targeted `WebSearch` calls
   against `sensirion.com` / `bosch-sensortec.com` got every fact needed at ~1% of the
   cost. **Lesson: match verification depth to how contested the facts are.** Killed the
   workflow at ~1.2M spent; finished with targeted search.

8. **Identify an SGP4x by its I2C address (or by the module's silkscreen / product listing).**
   ❌ Rejected 2026-08-01, after the address probe gave a confidently wrong answer. The
   **SGP40 and SGP41 both ACK at 0x59** — the SGP41 is a pin-, package- and
   address-compatible upgrade — and **both pass the same `0x280E` self-test**. Our
   autodetect assumed SGP40 at 0x59, so a node would log `SGP40 self-test PASS` while
   sitting on either part: a claim the firmware had no evidence for. The parts are not
   interchangeable in software — `measure_raw` is `0x260F` on the SGP40 and `0x2619` on
   the SGP41, and **neither implements the other's** — so guessing wrong means a dead gas
   lane, and a silkscreen is just a human's claim about a cheap module.
   ✅ What works: ask the part. `sgp4x_identify()` issues the SGP41-only `0x2619` and
   requires an ACK *plus* two CRC-valid words, falling back to the SGP40's `0x260F`.
   *Also rejected:* using `get_featureset` (`0x202F`, low 9 bits: `0x20`=SGP40,
   `0x40`=SGP41) as the **authoritative** test, which is what most libraries do. It is
   documented in **neither** datasheet's command table, and newer die revisions have
   shipped values that broke exact-match checks in the wild (esphome/issues#5995). We
   read it and **log** it as corroboration, but the behavioural probe decides.
   **Lesson: when two parts are deliberately drop-in compatible, identity must be proven
   behaviourally — compatibility is exactly what defeats identification by inspection.**

---

## 6. Normalization verdict & recommended fusion strategy

**Can these three be normalized onto ONE directly-comparable scale?**
**Partially, and only honestly.** They cannot share an absolute physical scale (only
SGP30 has one). They *can* share a **common banded cleanliness scale (5 bands + 0–100
numeric)** if we accept — and surface — that most rooms are scored **relative to their
own rolling baseline**, not against each other in absolute units.

**Recommended strategy — "relative-first, absolute-anchored where the hardware allows":**

- **Canonical output:** a 5-band scale **Good / Fair / Moderate / Poor / Very Poor**
  (anchored on the UBA five-level structure, §3.4), with a **0–100 numeric** underneath.
  Keep "higher = cleaner" to match the existing `air_quality` convention (no competing
  metric) — banding maps monotonically onto it.
- **Per-family transfer functions:**
  - **SGP30** → the one family that can be scored **absolutely**: map TVOC ppb through
    the UBA bands (with baseline-drift caveat + both-floor exclusion). Contributes an
    *absolute* band.
  - **SGP40** → score VOC Index against the **sensor's own rolling distribution**
    (relative percentile → band). Contributes a *relative* band.
  - **BME680** → normalize **raw gas_ohm** against a rolling clean-air baseline
    (reuse `gas_compensation.py`'s humidity-compensated shape, but avoid the 0–100
    ceiling clamp), → band. Contributes a *relative* band.
- **Per-reading metadata (mandatory for honesty):** each unified reading carries a
  `basis` flag (`absolute` | `relative`), a `confidence`/quality flag (warmup, burn-in,
  stale-input, invalid), and the contributing raw signal(s). This is what powers the
  Phase-6 explanation layer and prevents the §5.5 dishonesty.
- **Freshness gating (§4.4):** the unified index must go stale/unknown if the *raw* input
  is stale, regardless of whether a derived value was written.

This is the input to **Phase 2 (design ADR)**, where the exact band thresholds,
rolling-window lengths, and transfer-function math get pinned down.

---

## 7. Adding a new gas sensor — a repeatable checklist

Turn the path above into steps for the next sensor (SGP41, ENS160, SEN5x, …):

1. **Classify the output's scale nature** — absolute concentration? relative
   self-calibrating index? raw physical quantity (Ω/counts)? This decides everything.
2. **Find the vendor's stated semantics** — ~3 targeted searches on the vendor domain +
   their GitHub. Specifically ask: is there a *sanctioned* absolute mapping, or is it
   relative-by-design? Do NOT fabricate a mapping the vendor withholds. (Do NOT reach for
   a heavy multi-agent research harness for documented spec — §5.7.)
3. **Characterize OUR real data** — run the §2 Workstream-B DuckDB view for the new
   device_ids: percentiles, cadence, warmup/floor artifacts, stationarity over the span,
   per-node absolute offsets. Ground-truth the datasheet against your actual units.
4. **Decide the basis** — absolute (has a real anchor like UBA) vs relative
   (own-baseline). Most VOC/MOX sensors are relative; label them so.
5. **Pick the transfer function** — absolute band table, or rolling-percentile/baseline.
   Prefer adaptive baselines (everything drifts). Avoid ceiling-clamped scores.
6. **Wire the honesty metadata** — `basis`, confidence/quality flags, contributing raw
   signals, freshness gate on the raw input.
7. **Add to `METRIC_CATALOG` + `NORMAL_RANGES`**, persist + backfill (compute-and-store).
8. **Update this document** with the new sensor's row and any new dead end you hit.

---

## 8. Sources

Manufacturer / primary:
- Sensirion Gas Index Algorithm (SGP40/41), README — 24 h baseline, gain-offset normalization: https://github.com/Sensirion/gas-index-algorithm
- Sensirion "VOC Index for Experts" application note (PDF): https://sensirion.com/media/documents/A6D12AD4/61644979/Sensirion_Gas_Sensors_Datasheet_GAS_AN_SGP40_VOC_Index_for_Experts_D.pdf
- Sensirion SGP40 datasheet: https://sensirion.com/media/documents/296373BB/6203C5DF/Sensirion_Gas_Sensors_Datasheet_SGP40.pdf
- Bosch BME680 datasheet: https://www.bosch-sensortec.com/media/boschsensortec/downloads/datasheets/bst-bme680-ds001.pdf
- Bosch BSEC datatypes (IAQ 0–500 definition): https://github.com/BoschSensortec/BSEC-Arduino-library/blob/master/src/inc/bsec_datatypes.h
- German Committee on Indoor Air Guide Values (UBA): https://www.umweltbundesamt.de/en/topics/health/commissions-working-groups/german-committee-on-indoor-air-guide-values

Reference / precedent:
- `raspi-bme680-iaq` — humidity-compensated 0–100 IAQ from raw gas resistance without BSEC: https://github.com/thstielow/raspi-bme680-iaq
- TVOC IAQ standards overview (UBA five-level, unit conversion): https://atmotube.com/blog/standards-for-indoor-air-quality-iaq

Internal:
- `server/gas_compensation.py`, `server/maintenance/gas_quality_persist.py` — existing BME680 derived `air_quality`
- `server/api/viewmodel.py` — `METRIC_CATALOG`, `NORMAL_RANGES`
- `firmware/components/{sgp30,sgp40,bme680,ha_gas}/` — firmware sources & published metrics
- `edge/esp32c6/nodes.yaml`, `edge/esp32s3-eth/nodes.yaml`, `instance/devices.yaml` — node→device→area registry
