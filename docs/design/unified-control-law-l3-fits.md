# L3 fits: filling the unified control law's gaps from data on file

Generated 2026-11-12 ~08:30 PST, read-only, from ha-2. Design: `docs/design/unified-control-law.md` stage L3.
All times Pacific (PDT before 11-01, PST after).

**Data sources.** ha-2 `rungs.db` (1-min rungs, 11-05 08:00 → now; 1-hour rungs), `weather.db` (Open-Meteo),
`control.db` (control_log), and the raw-reading archive (`instance/db/parquet`, read with ha-2's
`venv/bin/python` + duckdb, plus `hot.db` for the last day).

**One data-quality finding first.** The **1-hour rungs before 2026-11-05 08:00 PST are unusable for
`thermostat_hall` and `hvac_blower`**. They were built from 2–6 samples an hour, and they disagree with the
supply-air temperature: hours read heat = 1.0 while the supply register sat at 21 °C, and hours read 0 while it
read 36 °C. Every fit below that goes back before 11-05 rebuilds per-minute values from the parquet archive,
where the thermostat reports about every 30 s. Anything that reads 1-hour rungs for HVAC state before 11-05
(dashboards, back-tests) inherits the problem.

Excluded windows (study §6b): spray foam 10-26 12:00 → 10-28 06:00, waxing 10-29 16:00 → 10-30 06:00, oiling
and manual boost 11-06 09:00–23:00, windows and forced fan 11-08 12:00 → 11-09 07:00, cooking 11-09 18:30–22:00.

---

## 1. House heat-loss model — confidence: **rough**

Model: `C·dTi/dt = Q_f·f_heat + P_dehum + G(time of day) − UA·(Ti − To)`.

- Ti is the mean of the 5 room meters.
- f_heat is the heat-call fraction, from raw `hvac_state`.
- P_dehum is Aprilaire `dehum_pm` plus Midea `plug_hall`, counted at full electrical power as heat.
- G is internal + solar gain, one constant per period: night 22–06, morning 06–10, midday 10–16, evening 16–22.
  There's no solar data in weather.db, so the period constants absorb the sun.
- Fit by block-averaged balance. Blocks are those 4 periods per day, weighted by length. The storage term uses
  the block's mean dTi/dt.
- Window: 10-31 00:00 (first real heat calls) → 11-12 06:00, 39 clean blocks.
- **Q_f is fixed from the furnace spec, not fitted** (see 1b). UA, C and G all scale linearly with Q_f.

Why the hourly dynamic fit fails: the thermostat holds Ti, so dT/dt barely moves (r² 0.05). The information is in
how much heat the thermostat has to call for, i.e. the steady-state balance.

### 1a. Results (Q_f = 11 kW)

| Outdoor reference | UA (W/K) | C (kWh/K) | τ = C/UA | Gains, night / morning / midday / evening (W) | r² |
|---|---|---|---|---|---|
| Open-Meteo | **145 ± 23** | 5.1 ± 1.3 | ≈ 35 h | 320 / 880 / 755 / 865 (± 270–340) | 0.71 |
| switchbot_outdoor | **208 ± 23** | 5.8 ± 1.1 | ≈ 28 h | 1260 / 1820 / 1420 / 1530 (± 260–340) | 0.82 |
| Daily balance, 9 full days (cross-check) | 143 ± 43 (OM), 196 ± 40 (sensor) | — | — | 600–1300 mean | 0.6–0.8 |

- **Recommended: UA ≈ 175 W/K (range 140–210), C ≈ 5.5 kWh/K (4–7), τ ≈ 30–35 h, mean gains ≈ 0.7–1.5 kW.**
- The two outdoor references bracket the answer. The sensor has a daytime microclimate bias and a wet bias. Open-Meteo
  is a grid model.
- **Independent check on C:** isolated heat calls (n = 28, mean 12 min) lift the room mean by +0.32 ± 0.02 K at
  +20 min (≈ +0.38 K against the pre-call trend). That gives C_fast ≈ 2.2 kWh / 0.38 K ≈ 5.8 kWh/K at 11 kW,
  consistent with the fit. The lift then decays: +0.18 at 60 min, +0.13 at 90 min.
- **Envelope sealing (11-07 18:00):** not separable. Only 12 blocks after it, at ΔT 5–10 K with f ≈ 0–0.05, and
  that fit collapses (UA −23 ± 49). The setpoint also changed 11-06 18:18. Refit once colder weather arrives.
- **ERV term:** adding (Ti − To)·cfm to the fit gives +2 to +16 ± 25–30 W/K per 100 cfm. That is consistent
  with the physics value of ≈ 20 W/K per 100 cfm at ε ≈ 0.65 (see §3), but not measured.

### 1b. Furnace

- **Stage:** low stage only. Zero stage-2 minutes since 11-05 (per-minute data). One hour of stage 2 on 10-26,
  during the thermostat intake.
- **Calls since 10-31:** 114. Duration median 10 min; 72 of the calls ran 10–14 min, 25 ran 5–9 min, 17 ran ≥ 15 min.
- **Delivered heat (spec-based, to verify on the rating plate):** the model number's "060" is 60 kBtu/h input on
  high fire. Low fire is taken as ≈ 40 kBtu/h input at ≈ 95 % AFUE, ≈ 38 kBtu/h ≈ **11 kW delivered**.
  - That is **≈ 0.18 kWh per heat-call minute**, ≈ 1.8–2.2 kWh per typical call.
  - Gas: ≈ 11.6 kW input ≈ 0.40 therm/h of call.
  - The docs don't hold the capacity. Principle 2 (docs-first) says to confirm it from the 59TP5A product data.
- **Checks on Q_f:**
  - Supply register (H office) peak rise over the return: 9–15 K, still rising at 20 min, because the meter lags.
    At the rated low-fire airflow (780 cfm) that gives a **lower bound of ≈ 6.5 kW**. The full rise implied by
    11 kW is ≈ 25 K.
  - Fitting dehumidifier heat as a free regressor gives the heat it displaces: Q_f ≈ 13 kW, but wide (9–24 kW).
  - Both are consistent with 11 kW.

### 1c. Heat-call fraction needed vs outdoor temperature

Ti = 22.4 °C (75 °F setpoint, room mean), dehumidifiers off. Each kW of dehumidifier heat removes ≈ 0.09 from f.

| Outdoor °C (°F) | f, Open-Meteo fit | f, sensor fit | Furnace min per hour |
|---|---|---|---|
| 15 (59) | 0.04 | 0.01 | 0–2 |
| 10 (50) | 0.10 | 0.10 | 6 |
| 5 (41) | 0.17 | 0.20 | 10–12 |
| 0 (32) | 0.24 | 0.29 | 14–17 |
| −5 (23) | 0.30 | 0.39 | 18–23 |

- Observed: 10-31 / 11-01 at To ≈ 5–6 °C ran f = 0.17.
- Balance point (f = 0) ≈ To 17–18 °C.
- The low stage alone covers the design load with a large margin.

---

## 2. ERV effect on air quality — confidence: **none** (index), **rough** (raw)

### 2a. The three nights

Mean over 00:00–06:00, and change from the same evening's 18:00–21:00 mean. SGP41 `voc_raw_comp` rises as air
gets **cleaner**. SGP30 tvoc in ppb.

| Night | ERV | c_office raw (Δ vs evening) | kitchen raw (Δ) | h_office raw (Δ) | c_bed tvoc (Δ) | Unified AQ, 6-node mean (excl. c_bed) |
|---|---|---|---|---|---|---|
| Mon 11-09 → Tue | min, 66 cfm | 31 200 (+336)* | 31 453 (+1044)* | 30 913 (+219)* | 524 (−312)* | 80.4 (87.7) |
| Tue 11-10 → Wed | **off**, 0 cfm | 31 315 (−78) | 31 672 (+59) | 31 032 (+71) | 443 (−67) | 81.4 (88.3) |
| Wed 11-11 → Thu | **high**, 125 cfm | 31 664 (+173) | 32 019 (+292) | 31 018 (+15) | 478 (+8) | 79.8 (86.5) |

\* The evening of 11-09 had the cooking event, so its "Δ" is recovery from cooking, not an ERV effect.

- **High vs off, overnight change:** c_office +251 raw, kitchen +233 raw (cleaner on the high night), H office
  −56, c_bed SGP30 +75 ppb (worse).
- **Night-to-night baseline swing:** ±300 raw (c_office evening level 30 864 → 31 394 → 31 491 across the three
  days). So high vs off is **about 1 baseline swing**: suggestive in 2 of 4 rooms, not significant.
- On the off night the raw signals stayed **flat**; they didn't fall. With nobody cooking overnight, the source is
  small and infiltration alone kept up.
- **The self-normalizing indexes show no ERV effect.** The unified AQ mean was 81.4 on the off night, 79.8 on high
  and 80.4 on minimum (|Δ| ≤ 2 points). The VOC indexes re-baseline within hours, so they can't see a sustained
  ventilation change.

### 2b. ERV flow vs recovery after VOC events

10-18 → 11-12. An event is a raw drop of ≥ 350 below the 1–3 h baseline. The measure is the time to recover 50 %
of the drop, against the mean ERV cfm during recovery.

| Room | Events (n) | Recovery rate (1/h) | Rate change per 100 cfm | r² |
|---|---|---|---|---|
| c_office | 17 | 2.8 ± 1.2 | −0.9 ± 1.2 | 0.04 |
| kitchen | 33 | 1.1 ± 1.3 | +1.5 ± 1.3 | 0.04 |
| H office | 17 | 1.7 ± 1.6 | +0.1 ± 1.7 | 0.00 |

- **No flow dependence.** Half-recovery is mostly 10–60 min at any flow. Even 125 cfm into the ≈ 520 m³
  conditioned volume is 0.4 air changes per hour (t½ ≈ 1.7 h). So recovery from short events is mixing and
  sorption, not ventilation.
- Other ERV level changes in the period were mostly set by the AQ rule itself (endogenous: low AQ → high ERV). The
  dry-boost-driven changes are too few for an event study.

### 2c. For the law

- **`aq_points_per_level` ≈ 0** (no measurable change on the unified index; |Δ| ≤ 2 over a 9 h night).
- Raw prior: ≈ +80 raw counts (cleaner) per ERV level overnight, in the c_office and kitchen sensors only, at
  ≈ 1 SD of the day-to-day noise.
- Physics prior for a **sustained** source (off-gassing days, 11-06 / 11-08): the steady-state excess scales as
  1/(λ_erv + λ_inf), so low → high (66 → 125 cfm) cuts it by roughly 30–45 % (λ_inf unknown, ~0.1–0.2 per hour).
- This argues for keeping the AQ term **soft**. The ERV does little for evening cooking spikes, and probably
  helps only for sustained off-gassing, which the data here can't confirm. An NDIR CO₂ sensor would settle it.

### 2d. C bed

gas_c_bed (SGP30) reads AQ ≈ 46 with the room unoccupied. It pulls the 6-node mean down ≈ 7 points (80.4 vs 87.7
without it). Its tvoc doesn't respond to the ERV either way. Drop it from the ERV's AQ source set, or weight it,
while C is away.

---

## 3. ERV sensible heat recovery — confidence: **rough** (ε), **good** (what the sensor reads)

### 3a. `supply_temp_c` reads the outdoor **intake** air, not the post-core supply

Steady running hours at night, 10-19 → 11-12, n = 204. The sensor tracks outdoor temperature (r² 0.98–0.99):

| Band | Fit | Apparent ε = (T − To)/(Ti − To) |
|---|---|---|
| 55–75 cfm (n 95) | supT = −2.1 + 0.74·To + 0.39·T_attic | 0.29 |
| 75–100 cfm (n 61) | supT = −1.7 + 0.79·To + 0.32·T_attic | 0.24 |
| 100–140 cfm (n 48) | supT = −1.2 + 0.91·To + 0.22·T_attic | 0.21 |

- The apparent ε **falls with flow**. That's the signature of outdoor air warmed by its duct and the casing in the
  ~22 °C conditioned attic, not of a heat-exchanger output. Post-core air at the rated 75 % would read ≈ 18–19 °C;
  it reads 11–17.
- The warming is ≈ +2.6 to +4 K, ≈ 150–190 W picked up from the attic, roughly constant across flows.
- At rest, the sensor drifts to attic temperature: 11-12 07:01–07:40 it went 10.8 → 20.8 °C, and on the off night
  it read +15.6 K over outdoor.
- So it's Broan's outdoor-air (defrost/schedule) sensor. **It can't measure core effectiveness.**

### 3b. ε measured another way

The air handler's supply − return temperature rise (§6) carries the ERV's cool fresh air. Over 59 Aprilaire-on,
blower-on, no-heat hours (11-09 12:00 → 11-12 06:00):

`ΔT = 1.40 + 0.29 ± 0.04 × (cfm/655)·(To − T_return)` (+ latent term), r² 0.68.

- If the H office supply meter sees the fully mixed stream at 655 cfm: 1 − ε = 0.29, so **ε ≈ 0.71 ± 0.04**.
- If the meter sees only ~0.7–0.9 of the rise (suggested by the constant term against the Aprilaire's ≈ 580 W):
  **ε ≈ 0.59–0.68**.
- **Use ε ≈ 0.65 (0.58–0.72).** The rated figure is 75 % at 32 °F (the "E75" in the part number).

### 3c. Heat cost per cfm in winter

- Air: 1 cfm ≈ 0.569 W/K.
- Heat cost ≈ 0.569·(1 − ε)·(Ti − To) ≈ **0.20 W per cfm per K** (0.16–0.24).
  - At 12 K: ≈ 2.4 W/cfm, so ≈ 160 W at low (66 cfm) and ≈ 300 W at high (125 cfm).
  - Gas equivalent ≈ 0.9–1.6 heat-call minutes per hour.
- Fan power, measured (median `power_w`):

  | Flow | Fan power |
  |---|---|
  | stopped | 7.7 W |
  | 66 cfm | 31 W |
  | 92 cfm | 57–59 W |
  | 125–130 cfm | 130 W |

  So high costs ≈ 100 W more electricity than low, on top of ≈ 140 W more ventilation heat at 12 K.

### 3d. The 11-12 stop was not a cold schedule

- **Not repeated.** LOW restarted at 07:41 (66 cfm, 31 W) and was still running at 08:14 PST (end of data). The
  stop predicted near 08:01 didn't happen.
- **The only zero-flow stop outside OFF mode in the whole archive.**
  - LOW has run 15 081 min since 10-19. 2 346 of those minutes had intake < 10 °C (1 512 below 9 °C), down to 5.7 °C,
    without stopping.
  - Of 37 switches into LOW, 24 came at intake ≤ 13 °C. All ran at 66 cfm, including 10-31 06:23 at 6.0 °C intake.
  - Among them, a boost-override end into LOW (10-28 06:43, 10.7 °C) ran normally.
- Broan's 19–50 °F "40 min/hr" schedule would have fired hundreds of times, and it belongs to the J13 `auto`
  sub-mode, which our RS-485 control doesn't use.
- **Verdict:** a one-off of unknown cause, not a cold schedule. The ERV model shouldn't include a duty cycle; keep
  watching for recurrence.

---

## 4. Furnace VOC reach into the rooms — confidence: **rough**

Window 10-26 → 11-12 (per-minute data from the archive). Classes as in `tools/furnace_closet_aq.py`:

- **A:** heat call with the blower off before (n = 63).
- **B:** heat call with the blower already running, i.e. combustion only (n = 33).
- **C:** blower start, no heat (n = 14).

Change from the 15 min before the start, 5-min mean around each offset, ± SE.

| Signal | Class | +10 min | +20 | +30 | +60 |
|---|---|---|---|---|---|
| **closet** SGP41 VOC index | A | **+102 ± 6** | +67 ± 10 | +39 ± 10 | +38 ± 11 |
|  | B | **+70 ± 6** | +42 ± 6 | +11 ± 6 | +3 ± 8 |
| **closet** SGP41 raw | A | −410 ± 73 | −281 ± 54 | −167 ± 44 | −140 ± 50 |
|  | B | −221 ± 11 | −128 ± 19 | −47 ± 23 | −54 ± 29 |
| c_office raw_comp | A | −17 ± 9 | −28 ± 14 | −33 ± 18 | −48 ± 37 |
|  | B | **−31 ± 5** | −19 ± 7 | −15 ± 9 | −15 ± 9 |
| c_office VOC index | A | +2 ± 4 | +10 ± 6 | +14 ± 8 | +12 ± 9 |
|  | B | **+8 ± 2** | **+10 ± 2** | +5 ± 2 | +6 ± 3 |
| c_office unified AQ | A | −0.3 ± 0.6 | −2.1 ± 1.0 | −2.7 ± 1.4 | −2.6 ± 1.8 |
|  | B | −1.2 ± 0.4 | −1.9 ± 0.4 | −1.1 ± 0.3 | −1.0 ± 0.5 |
| kitchen raw_comp | A / B | +20 / −9 | −30 / +30 | −51 / −1 | −40 / −47 (all ±16–49, noise) |
| kitchen VOC index | A / B | −3 / +2 | +7 / −5 | +14 / +2 | +8 / +13 (±3–11, noise) |
| H office SGP41 raw | A / B | +1 / **+40** | +5 / +48 | −17 / +14 | −36 / +16 (cleaner, not worse) |
| H bed / H office BME680 gas Ω | A / B | within ±1 % | | | (noise) |
| c_bed SGP30 tvoc | B | +14 ± 4 | +3 ± 5 | +15 ± 10 | +21 ± 13 |

- Class C (blower only) makes the rooms read *cleaner* (c_office index −13 to −26), so A − C overstates things. The
  C sample is small (14) and comes from different times of day.
- **Reading:**
  - The furnace's VOC reaches **C office only, at about 10–15 % of the closet signal**: index +8 to +10 for 20 min
    on combustion-only calls, ≈ −1 to −2 unified-AQ points, gone by 30–60 min.
  - Kitchen and H office: nothing detectable. H office reads cleaner, consistent with supply-air dilution.
  - The C office branch may sit near the closet or the return. Its SGP41 is the room sensor most coupled to the
    air handler.
- **For the law:** the furnace AQ term in the rooms is **≈ −1 to −3 AQ points for ~30 min, C office only**. A
  pre-emptive ERV / purifier push on heat calls isn't justified by this. Keep it off (the design's default).

---

## 5. Purifiers — confidence: **none** for a PM response model

| Unit | PM2.5 populated? | Plausible? |
|---|---|---|
| purifier_living_room | **No.** Reports **983** (saturated / fault) from 10-19 18:00 to now. Real readings only 10-18 17:01 → 10-19 17:52 and in a few brief windows (10-30, 11-02, 11-05) | Unusable; hardware or sensor fault (dust in the optical path, or a stuck sensor) |
| purifier_c_office | Yes, 5-min cadence | Yes. Floor at 1 µg/m³; spikes 8–49 (10-24 10:11 25, **10-27 16:26 49** during the foam days, 11-01 21, 11-08 10 when the windows opened) |
| purifier_c_bed | Yes | Yes. Floor at 1; spikes 8–28; **saturated at 983 for hours 10-27 19:35 → 10-28 ~11:00** (spray-foam window, real) |

- Speeds are set by the **VOC-based AQ rule** ("sensor 55 → speed 2", etc.), not by PM. c_bed is pinned at speed 2.
- PM sits at the 1 µg/m³ floor > 95 % of the time, so speed-vs-PM response can't be measured: 7 c_office spikes,
  with no speed variation to compare.
- **For the law:**
  - purify_air has particle effect only.
  - The living-room unit's PM must be marked invalid (983 = fault) until repaired.
  - A PM-driven rule is possible for c_office / c_bed, but there's no response model.

---

## 6. Method-B dips in the Aprilaire L/kWh — confidence: **rough, partly explained**

Window: 11-09 12:00 → 11-12 06:00, 59 hours with the Aprilaire on, the blower on, and no heat in the prior 25 min.
- Approximate zero model: +0.069 + 0.115·(T_ret − 21.5) + 0.054·cfm/100·(W_out − W_ret), with outdoor from hourly
  Open-Meteo. It reproduces the study's dips: 11-09 15–16, 11-10 18–19, 11-11 10, 11-11 17 / 19–21, 11-12 00–04;
  13 dip hours (< 0.5 L/kWh) out of 59.

**Candidate explanations** (dip-hour mean vs normal-hour mean; r with L/kWh):

| Candidate | Dip | Normal | r | Verdict |
|---|---|---|---|---|
| Return meter vs house-mean AH (RH drift) | −0.424 | −0.409 g/m³ | +0.11 | **no**: the return meter tracks the rooms the same in dips |
| Supply meter vs H office room (BME680) AH | +0.02…+0.24 | +0.0…+0.3 | — | **no** clear drift |
| Outdoor temperature (sensor / Open-Meteo) | 11.3 / 13.1 | 12.0 / 13.8 °C | −0.01 / +0.02 | **no**; falling outdoor (3-h ΔT) r +0.06 |
| Kitchen − house AH (cooking moisture) | −0.30 | −0.21 | +0.40 | **no**: the kitchen is *drier* in dips |
| Attic temperature | 22.2 | 22.3 | +0.12 | no |
| Aprilaire power | 586 | 579 W | −0.47 | real but tiny (+7 W) |
| Blower | always on (selection) | | | n/a |
| **ERV flow** | **100** | **68 cfm** | **−0.54** | **partly**: mean L/kWh 0.80 (0 cfm) → 0.72 (55–80) → 0.60 (80–110) → **0.42 (110–130, 6 of 9 hours are dips)** |
| ERV intake temperature | 14.3 | 17.0 °C | +0.38 | goes with flow and cold |
| **Outdoor reference: sensor dp − Open-Meteo dp** | **+2.7** | **+1.1 °C** | | **partly**: 12 of 13 dip hours have the sensor at ≥ 95 % RH. Open-Meteo's hourly dp jumps 3–4 °C hour to hour on 11-11/12 (11.9 → 8.8 → 6.9 → 5.7), so the ERV term swings with it |
| Supply − return temperature rise | 1.09 | 1.34 K | +0.59 | **not independent evidence** (below) |

**The temperature-rise clue is mostly the ERV, not less latent heat.**
- ΔT across the air handler fits the ERV sensible term at 0.29 ± 0.04 × (cfm/655)·(To − T_ret) (§3b). Dips come
  at higher flow and colder intake, so the ERV's cool air explains most of the 0.25 K drop.
- Including that term, the latent term's coefficient is weak (0.26 ± 0.10, attenuated by noise in the removal
  estimate).
- So ΔT can't confirm the removal fell.

**Sensitivity:**
- Taking outdoor AH from the sensor instead of Open-Meteo lifts the dip hours by +0.05 to +0.38 L/kWh, most on the
  11-11/12 high-flow nights (e.g. 11-11 20: 0.19 → 0.57; 11-12 03: 0.19 → 0.42). It doesn't lift 11-09 15–16
  (0.24 → 0.29), where the two agree.
- Erasing the dips through the ERV term alone would need outdoor air 3–7.6 g/m³ wetter than the return. That's
  implausible, so g or the outdoor reference can't be the whole story.

**Measurement floor (likely the remainder).**
- The study meters report **RH in whole percent** (verified in hot.db: return {40, 41}, supply {38…41}). At 22–24 °C,
  1 % RH ≈ 0.20 g/m³ ≈ **0.4 L/kWh** at 580 W and 655 cfm.
- The whole method-B signal is ~2 quantization steps. The return meter sits on one integer for hours (hourly
  means of exactly 40.0 / 41.0), so a sub-step drift of the true value shows up as a multi-hour episode of
  ±0.2–0.4 L/kWh.

**Best explanation, in order:**
1. The ERV term at high flow combined with an unreliable outdoor reference on wet-sensor nights. This covers the
   11-11 19–21 and 11-12 00–04 episodes.
2. 1 %-RH quantization of the study pair. This covers the residual and the 11-09 15–16 and 11-11 10 episodes,
   where nothing else moves.
3. Possibly real: none of the room-level evidence (house AH) separates a real removal drop from these.

Not supported: room-meter RH drift, cooking, falling outdoor temperature, attic, blower state.

**Suggested checks:**
- Score the hours with the sensor outdoor reference *and* with Open-Meteo, and report both.
- Exclude hours where the two disagree by > 2 °C dp at > 100 cfm.
- Add a 0.1 %-resolution meter (e.g. SHT4x on an ESP) at the return and supply. That fixes the quantization floor.

---

## Caveats common to all

- Q_f (11 kW) is a spec assumption. UA, C and the gains scale with it.
- Open-Meteo is a grid model; switchbot_outdoor is unshielded, with a wet bias at ≥ 95 % RH and a daytime warm bias.
- Only ~12 days of usable heating data, all mild (To 3–17 °C). Extrapolating below ~3 °C is model-only.
- Envelope sealing (11-07) and the setpoint change (11-06) fall inside the window and can't be separated.
- Single nights for the ERV AQ test; occupancy and cooking differ by day.

```json
{
  "model_params": {
    "generated": "2026-11-12T08:30-08:00",
    "window": {"house": "2026-10-31..2026-11-12 (per-minute thermostat from parquet; 1-hour rungs before 11-05 unusable for HVAC state)",
               "erv_aq": "nights 2026-11-09/10 (min), 11-10/11 (off), 11-11/12 (high); events 10-18..11-12",
               "erv_temp": "2026-10-19..2026-11-12", "furnace_voc": "2026-10-26..2026-11-12", "method_b": "2026-11-09 12:00..11-12 06:00 PST"},
    "house": {
      "ua_w_per_k": 175, "ua_range_w_per_k": [140, 210],
      "ua_by_reference": {"openmeteo": [145, 23], "sensor": [208, 23]},
      "c_kwh_per_k": 5.5, "c_range_kwh_per_k": [4.0, 7.0],
      "tau_h": 32, "tau_range_h": [28, 40],
      "internal_gain_w": {"mean": 1000, "range": [650, 1500],
                          "by_period_openmeteo_w": {"night_22_06": 320, "morning_06_10": 880, "midday_10_16": 755, "evening_16_22": 865},
                          "by_period_sensor_w": {"night_22_06": 1260, "morning_06_10": 1820, "midday_10_16": 1420, "evening_16_22": 1530}},
      "dehum_heat_fraction": 1.0,
      "erv_extra_ua_w_per_k_per_100cfm": {"fit": [9, 28], "physics": 20},
      "balance_point_outdoor_c": 17.5,
      "heat_fraction_vs_outdoor_c_at_ti_22_4": {"15": 0.03, "10": 0.10, "5": 0.18, "0": 0.26, "-5": 0.34},
      "per_call_room_rise_k": {"mean_call_min": 12, "peak_k": 0.32, "at_60min_k": 0.18}
    },
    "furnace": {
      "furnace_kw": 11.0, "furnace_kw_basis": "spec assumption: 59TP5A060 low fire ~40 kBtu/h input x ~95% AFUE; verify on rating plate",
      "furnace_kw_lower_bound": 6.5, "furnace_kw_dehum_crosscheck": [13, 9, 24],
      "kwh_per_call_minute": 0.183, "gas_kw_input": 11.6, "stage2_used": false,
      "call_minutes_median": 10, "min_on_min_typical": 10,
      "voc": {"closet_index_10min": {"blower_starts": [102, 6], "blower_running": [70, 6]},
              "c_office_index_10_20min": {"blower_running": [9, 2], "blower_starts": [10, 6]},
              "c_office_aq_points": [-2, 1], "kitchen": "none", "h_office": "none (cleaner)",
              "room_reach_fraction_of_closet": 0.12, "duration_min": 30}
    },
    "erv": {
      "sensible_eff": 0.65, "sensible_eff_range": [0.58, 0.72], "sensible_eff_rated": 0.75,
      "sensible_eff_basis": "air-handler dT fit: (1-eps)*k_sense = 0.29 +/- 0.04; k_sense 0.7-1.0",
      "supply_temp_c_reads": "outdoor intake air (+2.6..+4 K duct/casing warming in the conditioned attic; drifts to attic temp at rest); NOT post-core",
      "heat_cost_w_per_cfm_per_k": 0.20, "heat_cost_range": [0.16, 0.24],
      "fan_power_w": {"0": 7.7, "66": 31, "92": 58, "125": 130},
      "cfm_by_level": {"off": 0, "low": 66, "med": 92, "high": 125},
      "aq_points_per_level": 0.0, "aq_points_per_level_bound": 2.0,
      "voc_raw_comp_per_level_overnight": 80,
      "voc_recovery_dependence_on_cfm": "none detected (r2 <= 0.04)",
      "cold_schedule_duty_cycle": null,
      "cold_stop_note": "single 40-min LOW stop 2026-11-12 07:01; no recurrence; 2346 LOW min at intake <10 C ran normally"
    },
    "purifiers": {
      "pm25_valid": {"purifier_living_room": false, "purifier_c_office": true, "purifier_c_bed": true},
      "living_room_fault_value": 983, "pm_floor_ugm3": 1, "speed_driven_by": "VOC AQ rule",
      "pm_response_model": null
    },
    "aprilaire_method_b": {
      "dip_explanations": ["ERV term at high cfm + Open-Meteo vs sensor outdoor-reference disagreement (dips: 2.7 C dp vs 1.1 normal)",
                           "1% RH quantization of the study meters (1 step ~= 0.4 L/kWh)"],
      "lkwh_by_erv_cfm": {"0": 0.80, "55-80": 0.72, "80-110": 0.60, "110-130": 0.42},
      "rh_resolution_pct": 1.0
    },
    "confidence": {
      "house_ua": "rough", "house_c": "rough", "furnace_kw": "rough (spec, unverified)",
      "heat_fraction_vs_outdoor": "rough (mild weather only, To 3-17 C)",
      "erv_sensible_eff": "rough", "erv_heat_cost": "rough", "erv_supply_temp_meaning": "good",
      "erv_aq": "none (index) / rough (raw)", "erv_cold_schedule": "good (absent)",
      "furnace_voc_reach": "rough", "purifier_pm": "none (living room invalid; others at floor)",
      "method_b_dips": "rough, partly explained"
    }
  }
}
```
