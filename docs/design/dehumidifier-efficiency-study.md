# Dehumidifier efficiency study → moisture control law (plan, 2026-10-31)

**Goal (Hugh, 2026-10-31):** learn how well each moisture-removal lever works as a function of the indoor and
outdoor environment, then fit a regression that becomes a **control law**: when to run the Midea, the Aprilaire,
both, or neither, to remove excess indoor moisture at the lowest energy cost.

**Status:** plan only. Nothing here changes live control until Phase 4, and that phase starts in shadow mode.

## 1. Questions

1. **Removal rate:** for each unit, how many grams of water per hour it removes, as a function of inlet
   temperature and inlet dew point (or RH), and fan speed.
2. **Efficiency:** grams of water per Wh for each unit, as a function of the same variables. Refrigerant
   dehumidifiers lose capacity steeply as the air gets cooler and drier. Ratings are given at 80 °F / 60 %.
   The house runs at 65–72 °F and 40–46 %, with the coil near frost in winter.
3. **House moisture budget:** what adds water (occupants, cooking, showers, crawlspace, outdoor air when outdoor
   dew point > indoor) and what removes it (the units, plus ventilation and leakage when outdoor dew point <
   indoor). On 2026-10-30/31, with 35 °F at 98 % outside (≈5.5 g/m³), the house lost ≈0.7 g/m³ overnight. The
   Midea was idle and the Aprilaire ran all night, and the amount the Aprilaire contributed is unknown.
4. **The third lever:** the ERV (`erv_attic`, 66–132 cfm, HA-controlled). When outdoor air is drier than
   indoor, ventilation is a dehumidifier. It costs fan power plus heating, and heating is not metered (the furnace
   is gas). The law should be able to choose it.

## 2. Data inventory (verified on ha-2, 2026-10-31)

| Variable | Source | Coverage | Notes |
|---|---|---|---|
| Indoor T/RH, per room | SwitchBot meters (`meter_*`, `meter_pro_*`) | Jan 7 → (living room Pro since Feb 19) | `meter_living_room` and `meter_pro_living_room` disagree by ~5 °F; pick one per room, document why |
| Outdoor T/RH | `weather.db` (Open-Meteo, hourly) | Jan 1 → | grid model, not the house. Calibrate against `switchbot_outdoor` (local, since Oct 4) |
| Crawlspace T/RH | `ometer_crawlspace`, `radon_crawlspace` | Jul 21 → / Mar 24 → | the crawl is a moisture source/sink, and radon tracks its exchange with the house |
| Midea state | `dehumidifier_living_room` mode/fan/target/onboard T+RH | Jun 22 → (mode since Jul 18; control log since Jun 24) | onboard RH reads 9–15 % low (hot.db `authoritative=0`); onboard T = inlet T |
| Midea power | `plug_hall` (metered) | Oct 19 → | before that: back-calculated, 811–835 kWh Jun 24–Oct 31, ±1.5 % vs meter (§6) |
| Aprilaire state | `dehum_attic` dh_call / unit_running / unit_rh_pct | Oct 6 → | unit RH = its inlet. **No inlet temperature.** Own duct loop since Oct 17 (erv-airflow doc) |
| Aprilaire power | `dehum_pm` | Oct 4 → | blower 87–88 W, compressor ~570 W (erv-airflow doc §2026-10-17) |
| ERV airflow | `erv_attic` supply/exhaust cfm, rpm, boost | Oct 4 → | |
| Furnace / blower | `thermostat_hall` hvac_state, `hvac_blower` | Oct 26 → | the blower follows `dehum_attic` (follow coupling) |
| Occupancy proxy | `gas_c_bed` eCO₂, bathroom RH spikes | Jul → | showers and cooking are moisture pulses; flag and exclude, or model |
| House volume | `instance/house-geometry.json` (floor plan, 22 px/ft) + ceiling 94 in | — | ≈ 349 m³ (see reference figures) |

**Reference figures (Phase 0 prep, 2026-10-31):**

| Item | Value | Source |
|---|---|---|
| House | floor plan outer wall ≈ 1,575 ft² (146 m²) × ceiling 94 in (Hugh, uniform) → **≈ 12,300 ft³ (349 m³)**, provisional: the listing says ~1,750 ft², and Hugh trusts our plan more; a careful re-measure may come later. Volume only scales the whole-house balance (method A); per-unit numbers from methods B/C don't use it. ERV 92 cfm ≈ 0.45 ACH, 132 cfm ≈ 0.64 ACH | `instance/house-geometry.json`; Hugh |
| Midea MAD50S1QWT | 50 pt/day and **IEF 2.01 L/kWh at the DOE test point (65 °F / 60 %)**. Bucket 4.25 gal / 15.9 L (Hugh: manufacturer figure confirmed). Airflow: no authoritative figure (an aggregator's 430 CFM is implausible for this unit) → calibrate in place (below) | ENERGY STAR #4512372 |
| Aprilaire E070 | 70 ppd @ 80 °F/60 %, 53 ppd @ 73 °F/60 %. **EF 2.1 / 1.8 L/kWh** at those points. Airflow 200 / 170 / 140 CFM at 0.0 / 0.2 / 0.4 in. w.c. (dry coil). 5.8 A. Discharge temperature rise 10–30 °F. **Dehumidifies only with inlet ≥ 50 °F AND dew point ≥ 40 °F** | Aprilaire spec sheet form 960 |
| Furnace Carrier 59TP5A060-14 | 5-speed ECM, 11×8 wheel, 3/4 HP. Air delivery (bottom return + filter) at 0.1 / 0.3 / 0.5 in. w.c.: tap 1 Red 785/655/530, 2 Org 1160/1080/1000, 3 Blu 1010/925/835, 4 Yel 1325/1255/1185, 5 Gry 1720/1620/1505 CFM. Rated heating airflow 1000 (high) / 780 (low) CFM @ 0.12 in. **Which tap the continuous fan (G) uses is unknown**: read it off the control board | 59TP5A-02PD product data |

**In-place airflow calibration (no spec needed).** A dehumidifier puts all its electrical power, plus the
latent heat of the water it condenses, back into the air as sensible heat. So with metered power P and the
inlet→outlet changes ΔT (rise) and ΔW (drop):  ρ·Q·c_p·ΔT = P + ρ·Q·h_fg·ΔW  →  **Q = P / (ρ·(c_p·ΔT − h_fg·ΔW))**.
Water removed is then ρ·Q·ΔW. That gives airflow for each unit from its own runs, cross-checked against the
Midea bucket (§3C). The same balance across the air handler (return → C bed supply, furnace off) yields blower
airflow, with the Aprilaire's metered power as the heat source plus the blower motor's own heat (unmetered,
~0.1–0.3 kW, an error term to bound).

**Winter limit worth knowing:** the house dew point on 2026-10-30/31 was ≈ 43–45 °F (7.0–7.6 g/m³). The E070's
40 °F dew-point floor is close, so on cold dry nights the Aprilaire may run its fan without being able to remove
water. Watch for that in the data, and gate it in the control law.

**Gaps that limit the study:**
- No **condensate measurement** (the true answer for removal rate).
- No Aprilaire inlet temperature.
- No Midea power for high fan.
- The Aprilaire has only fall data, and its rest-of-year data starts now.

## 3. Methods

**A. Whole-house moisture balance (existing data, no hardware).**
Volume-weighted indoor absolute humidity `W_in`. Model it in 5-min steps:

    V·dW_in/dt = Q_leak·(W_out − W_in) + Q_erv·ε·(W_out − W_in) + S_people + S_events + S_crawl
                 − R_midea(T, W_in, fan)·on_midea − R_apr(T, W_in)·on_apr

Q_erv comes from cfm; ε is the ERV's moisture transfer and is an unknown to fit. Fit Q_leak, S, and the removal
functions R by regression over every on/off transition. Each start and stop is a natural step response.

- Strength: the history is already there, Jun 24 → for the Midea.
- Weakness: confounding (units often run together, and weather moves at the same time), and the result has to
  be averaged over the whole house.

**B. Across-unit psychrometrics (adds sensors; the cleanest removal measurement).**
One T/RH meter at the inlet and one at the outlet of each unit. Removal = ρ·airflow·(W_inlet − W_outlet).
- Needs each unit's airflow, from the manual or one measurement. Docs-first: get the figures from the Midea and
  E070 manuals, not from memory.
- Gives instantaneous removal at every condition, which is exactly what the regression needs.
- Cost: 4 meters (fewer if spares exist). The Aprilaire inlet meter also fills the missing inlet temperature.

**C. Condensate truth — the Midea bucket is already a meter (Hugh, 2026-10-31).**
Every bucket-full event (Midea P2) is a known volume of water. The controller logs it as
`interlock tank_full,error` on every tick until the bucket goes back in, so the last entry of each episode is
the emptying time. Hugh sometimes pulls the bucket before it's full. That shows up as an error without
tank_full (the unit's E6, compressor stopped for the pull), logged as `interlock error`.

A **clean bucket** is the stretch from any emptying (P2 or E6) to the next P2, with no E6 in between. Its
compressor hours and kWh (from the back-calculation) are a direct removal measurement once the bucket volume
is known.
- First pass, 2026-10-31: 26 P2 and 28 E6 episodes (Jun 26 → Oct 28) give **24 clean buckets**.
  - Median ≈ 32.7 compressor h ≈ 15.5 kWh per bucket.
  - Jun–Aug ≈ 29.6 h (living-room dew point ≈ 10 °C); October ≈ 34.6 h (≈ 8.7 °C).
- Outliers to explain:
  - Jul 2–4: 15–17 h.
  - Jul 12, Oct 11, Oct 19: 41–47 h.
  - Aug 31 → Sep 14: 249 h. That is impossible for one bucket, so there must be an unlogged dump or a drain hose.
- **Bucket:** 4.25 gal = 15.9 L nominal (Hugh, from the published spec). The P2 float may trip below the brim,
  so one measured full bucket would firm this up. At 15.9 L per bucket:
  - **Jun–Aug:** 0.54 L per compressor-hour, **1.14 L/kWh** (median 29.2 h, 13.9 kWh per bucket).
  - **Sep–Oct:** 0.46 L per compressor-hour, **0.97 L/kWh** (median 34.3 h, 16.3 kWh per bucket).
  - So efficiency fell ~15 % as the living-room dew point fell ~1.2 °C and the inlet cooled ~1 °C.
- **Done 2026-10-31:** the controller now publishes the unit's raw `error_code` and `tank_full` (0/1) on every
  poll, so from now on P2 vs E6 come from the device instead of being inferred.

Still check method B against this. The Aprilaire drain, if reachable, can be spot-checked the same way.

**D. Designed experiments — the automatic rotation (approved 2026-10-31; design below is the proposal).**
Natural operation mixes everything together (units run together, weather moves at the same time), so a
scheduler rotates the levers in fixed blocks, around the clock, for several weeks.

*Arms (one per block):*

| Arm | Midea | Aprilaire (+ furnace blower) | ERV | Notes |
|---|---|---|---|---|
| 0 Neither | off | off | base level | **baseline** — how fast the house gains or loses water by itself; subtracted from every other arm |
| M Midea | on (low fan) | off | base | |
| A Aprilaire | off | on | base | blower follows, as in production |
| MA Both | on | on | base | |
| E ERV boost | off | off | boost | only when outdoor dew point < indoor dew point − 2 °C; otherwise it would add water |
| M+ Midea high fan | on (high fan) | off | base | occasional; also settles the high-fan power question in the back-calculation |

*Blocks:*
- 90 min each: the first 30 min are discarded (compressor start-up, coil wetting, the house responding), and
  the last 60 min are measured.
- One cycle = every eligible arm once, in a **new random order each cycle**, so no arm is always at night or
  always during cooking. That's ≈7.5 h per cycle, ≈3 cycles a day.
- Runs **24 h a day** (Hugh, 2026-10-31).

*Furnace blower:* it **follows the Aprilaire, as in production** (Hugh: better circulation, and that is how it
will run). Consequences:
- The Aprilaire arms include blower mixing and the others don't. That's production-faithful, so the results
  apply directly.
- The whole-house balance (method A) will see Midea-dried air spread more slowly in arm M than in MA. The
  across-unit meters (method B) don't care.

*Guardrails* (a block ends early; the early end is logged and is itself data — the rise rate in arm 0 is the
house's moisture load):
- House RH **above 47–48 %** (Hugh, 2026-10-31) ends the block, and the scheduler runs the cheapest drying arm
  next. Measured on the living-room sensor that production control uses, with the house mean logged alongside.
- 35 % hasn't been reached in the history, so the low guard is only a safety backstop.
- A full Midea bucket (P2) ends an M, MA or M+ block.
- Every unit keeps its own protections: interlocks, minimum on/off times, the Aprilaire's low-inlet-temperature
  gate.
- Any PWA command to a study device pauses the study for that device, the same way a press releases the
  follow coupling. A **"pause study" scene** stops everything at once.

*Flagged, not interrupted* (excluded at analysis time): showers (bathroom RH spikes), cooking (kitchen VOC),
furnace heat runs (kept, but as a regressor: the firing fraction; see the furnace note in §6b), and events Hugh labels.

*Mechanism:*
- A scheduler module in ha-controller, driving the existing override path (the same one as a PWA override)
  rather than a new actuator path.
- Each block is a row in a control.db study table: arm, planned and actual start and end, how it ended, and
  inlet conditions at the start.
- It runs with no Claude session involved ("long tests must be code-driven"), and the PWA shows that a study
  is running and which block.

*Duration:* several weeks, covering cold-dry and mild-damp spells. Summer is covered by the Midea's history
(the bucket data in method C) until next year.

*Study meters (Hugh, 2026-10-31):*
- 3 outdoor-type temperature/RH meters on hand, 3 more arriving 2026-11-01.
- **Midea inlet + outlet first (2026-10-31).**
- **HVAC tomorrow:** one in the return (intake) grille and one in a supply duct (C bed).
- Return → supply measures the whole air handler. Removal = furnace blower airflow × ΔW (from the 59TP5 manual
  for the fan speed in use). That holds with heat on too: the heat exchanger is sealed, so the supply air gains
  heat but not water. (The furnace can still dry the *house*; see the furnace note in §6b.)
- Still to confirm: where the Aprilaire's own loop joins the HVAC ducts, so the supply meter sits downstream of
  where its air comes in.
- These meters get registered as **study devices**: in the HA network and recorded, but kept out of the PWA's
  regular room views. Recipe: register on ha-2 with `tools/intake_sensor.py register` (area = the unit's
  room), then set the control.db overlay to hidden with a "Study: …" name. Hidden devices drop out of
  build_sensor_list, the panels and the room rollups.
- **Registered 2026-10-31 (14:56–14:58 PDT):** `study_midea_inlet` and `study_midea_outlet` (switchbot_meter,
  living_room, hidden). Both record; neither is listed by /api/v1/panel/sensors. Ignore their first readings
  after power-on, until they settle (Hugh).

## 4. Analysis

1. Build the dataset in 5-min bins:
   - indoor W and dew point per room plus the house average;
   - outdoor W, T and dew point (Open-Meteo calibrated to `switchbot_outdoor`);
   - crawl W;
   - per unit: state, power and inlet conditions;
   - ERV cfm, furnace state;
   - event flags (shower, cooking, windows, spray foam on 10-26, waxing on 10-29).
2. Segment each unit's runs. Per run: Wh, removal (method B, or A as the step-change estimate), mean inlet T and
   dew point.
3. Fit removal and efficiency surfaces per unit:
   - expected form: g/Wh = f(T_inlet, Td_inlet), roughly linear in (Td_inlet − T_coil) above a cutoff;
   - include fan speed for the Midea;
   - report confidence intervals, and say plainly where the data is thin (winter for the Aprilaire, high fan).
4. Cost per gram for each lever at current conditions. The ERV's cost includes the heating penalty, estimated
   from cfm × ΔT × hours, since gas isn't metered (or ignored in summer).

## 5. Control law (target shape, Phase 4)

- **Control on indoor dew point**, or RH at the coldest surface, rather than living-room RH.
  - RH swings with temperature even when the water content doesn't move, as on the 10-30 cold night.
  - In winter the ceiling comes from window condensation: at 68 °F and 44 %, the dew point is ≈45 °F.
- **Each tick:**
  1. Compute the excess water: house W minus the target W.
  2. Estimate the cost per gram of each lever from the fitted surfaces.
  3. Run the cheapest lever(s) needed to clear the excess within a time horizon.
  4. Keep the existing interlocks, minimum on/off times, and the Aprilaire's low-inlet-temperature (E8) gate.
- **Probably-true outcomes to test:**
  - In winter, ventilating with dry outdoor air beats both compressors.
  - The Midea (indoors, warmer inlet) beats the attic Aprilaire when it's cool.
  - Running both only pays above some excess threshold.
- **Rollout:** shadow mode first, like the AQ v2 shadow. Log what the law would have done next to what the
  hysteresis rules did, compare kWh and dew-point tracking, then switch over one device at a time.

## 6. Phases

| Phase | Work | Needs Hugh |
|---|---|---|
| 0 | ✅ 2026-10-31 — [findings + decisions](dehumidifier-efficiency-study-phase0.md) (`tools/dehum_audit.py`): room set for the house average, local outdoor primary, Aprilaire state from power, Midea onboard T +0.84 °C. Ceiling 94 in, specs in §2. Still open: study-pair zero offset (a day of idle), HVAC meters + continuous-fan tap (2026-11-01) | outdoor meter sun? |
| 1 | First pass ✅ 2026-10-31 — [findings](dehumidifier-efficiency-study-phase1.md): an air-only balance can't measure removal (the house buffers ≈26× its air volume). Per-unit efficiency → B + C. A becomes a two-node house model fitted with B/C removal as known inputs | — |
| 2 | Install inlet/outlet meters (method B); optional condensate spot checks (C). Gates G2a/G2b in §6c | sensors on hand? placement |
| 3 | Designed-experiment schedule (D), several weeks across weather | OK to rotate the units automatically |
| 4 | Fit surfaces → control law → shadow → switch over | approve switch-over |

**Already done:** Midea energy back-calculation (2026-10-31). The plug meter showed three clean levels: mode 2 =
compressor 464 W + 8.8 W/°C × (T − 21 °C); mode 1 = 1.4 W; and a ~35 W fan run-on. The control log matches the
reported mode 99 % of the time, and the estimate is within 1.5 % of the meter over Oct 19–30. Total: 811 kWh
(835 kWh with a 30 W allowance for high fan) from Jun 24 to Oct 31, about 6.2 kWh/day.

## 6b. Study log (labelled events the analysis must respect)

- **2026-10-31 16:21 PDT: Midea powered OFF at its plug** (`plug_01` relay off; its automation `enabled=false`)
  — Hugh: the Aprilaire is doing the work fine. From here the regime is "Aprilaire only". The Midea study
  meters now see idle air, which is a free zero-offset calibration. To restore: `tools/plug_cmd.py plug_01 on`,
  then re-enable its policy.
- **2026-10-31 ~16:20 PDT: mech-closet USB-C splitter replaced** with Hugh's spare (sgp41_mech had been
  restarting every 40–80 s). Compare broker reconnects for .116 (sgp41_mech) / .111 (standby_c6) before and after.
- **Outdoor meter placement** (Hugh): under the garage's north overhang, never in direct sun. So its midday
  +2–3 °C over the grid is real microclimate (wall/garage heat), not solar loading.
- 2026-10-26 ~17:00 PDT spray foam; 2026-10-29 ~17:00 PDT oak-plank waxing (mineral spirits). Both are VOC
  events, not moisture events, but exclude the hours in case of window opening and ventilation changes.
- 2026-10-31 15:00–15:20 PDT kitchen leak-detector wet tests (seconds of water on a cloth; negligible).
- **Night of 2026-10-31 → 11-01 (first winter-regime night, Midea off):**
  - Midea study pair idle zero offset: outlet − inlet = **−0.49 g/m³ AH** (−2.9 % RH, +0.10 °C).
    The 1-min SD is 0.40; hourly means run −0.3 to −0.7. That is about 20–25 % of the expected running signal,
    so it must be subtracted. A position-swap test would separate sensor bias from placement.
  - Outdoor dew point was 2–7 °C, against 8.7–9 °C indoors; outdoor AH was 5.6–7.5 g/m³ against 8.1 indoors.
    The crawl (≈7.6 g/m³) was also drier than the house. So ventilation and leakage were drying the house, and the
    ERV (66–92 cfm, ≈52 W) acted as a dehumidifier. The control law needs ventilation as an actuator in this regime.
  - Aprilaire: call ON 18:19 PDT at LR 46.0 %, OFF 22:08 at 43.6 % (3 h 49 min, ≈2.0 kWh, 527 W while running).
    House-average AH fell 8.26 → 7.98 and rebounded to 8.18 by midnight: the buffering again.
    The unit's own RH sensor read 36–39 % while the LR Pro read 44–46 %.
  - Heat ran 15–40 % of each hour. The coldest room was 20.1–20.6 °C. The heat exchanger is sealed, so the
    supply air gains no water from heating, and a
    return→supply ΔAH taken during a heat call with the Aprilaire off is a zero reference for the HVAC pair.
- **2026-11-01 13:21–13:28 PST: HVAC study pair registered** (hidden, switchbot_meter): `study_hvac_return`
  (return grille, area hall) and `study_hvac_supply` (**H office** supply register, Hugh's choice instead of C bed).
  Discard their first 10–60 min while they settle. Still open: whether the H office branch is downstream of
  where the Aprilaire's loop joins the ducts.
- **Duct topology (Hugh, 2026-11-01).**
  - Supply: an ~18 in. **rigid aluminum** round trunk loop around almost the whole house perimeter (~8 m³ of air;
    transit ≈ 20 s at blower flow, ≈ 1 min on ERV + Aprilaire flow alone); each register hangs off it on a short 4 in. flex run (≈1–7 ft) into a
    standard rectangular register box. Return: an ~18 in. round duct into the mech closet, then rectangular stack
    through the furnace/blower and back out to the supply loop.
  - The **Aprilaire and the ERV both draw from the return just above the ceiling grille** and both **discharge
    at the round-to-rectangular supply transition**. There is no backdraft damper on the furnace stack, so with
    the blower off their discharge splits between the supply loop and back through the furnace cabinet toward
    the return, where the Aprilaire can re-ingest its own dry air. That's why the Aprilaire runs paired with the
    blower.
  - Consequences for method B:
    1. The return-grille meter is effectively the Aprilaire's (and the ERV exhaust's) inlet.
    2. The supply meter sees the Aprilaire **and the ERV's fresh air** mixed into the blower stream. The ERV
       runs all the time at 66–132 cfm with outdoor-driven moisture, so the "zero" from blower-on / Aprilaire-off
       minutes includes an ERV term ≈ (Q_erv / Q_blower)·(W_erv,supply − W_return). Tonight that's ~−0.1 g/m³,
       the same size as the precision target. It has to be modelled (cfm × moisture difference) or measured.
    3. Rigid metal doesn't exchange moisture, so supply ΔAH holds at any register. The ducts run in the attic,
       whose environment is `meter_attic` (Hugh): 21–23 °C / 39–42 % over 2026-10-31→11-01 with 2–10 °C outdoors,
       i.e. a **conditioned attic inside the envelope**. So the loop loses almost nothing in fan-only operation
       (air ≈ attic temperature) and only the heat-call excess during heating, and that heat stays in the house.
       Register ΔT is usable with the blower on and the furnace idle; during heat calls, treat it with caution.
    - The attic air (≈ 7.8 g/m³ vs ≈ 8.1 in the rooms) is part of the conditioned volume, and its materials part of
      the buffer. Method A's V = 349 m³ (rooms only) undercounts; add the attic volume when the two-node model is built.
    4. A meter in the Aprilaire's own discharge, with the return meter as its inlet, measures it directly from
       its own ~140–200 cfm (≈5× the signal, and a well-conditioned in-place airflow). A meter in the ERV's
       fresh-air delivery measures ventilation drying directly. With both, the return→supply pair becomes a
       cross-check instead of the primary measurement.
- **Furnace note (Hugh, 2026-11-01: combustion and exhaust can dry a space).** Correct at the house level, even
  though the supply airstream gains no water. Three paths, all strongest when it's cold and dry outside:
  1. *Combustion air.* The 59TP5 is (by its 59-series prefix, to confirm from the manual) a condensing furnace,
     installable as direct-vent (two pipes, outdoor combustion air) or one-pipe (combustion air drawn from the
     space). If one-pipe, it exhausts roughly 7–11 cfm of indoor air while firing, made up by drier outdoor air.
     That's about 40 g/h at high fire with tonight's 2 g/m³ difference. The combustion water itself leaves in
     the flue gas and the condensate drain.
  2. *Stack effect.* Infiltration rises with the indoor–outdoor temperature difference, which is also what makes
     the furnace run. So a leakage term that ignores ΔT will wrongly hand credit to the furnace.
  3. *Duct leakage.* With the blower running, leaky attic ducts pressurize or depressurize the house and drive
     extra exchange. The blower is paired with the Aprilaire, so this confounds the A arms with the others.
  - Model consequence: the house balance gets a leakage term scaled by ΔT (stack) and by blower-on, plus a
    furnace firing-fraction term × (W_out − W_in). The across-unit (B) measurement is unaffected.
  - To check: is the furnace two-pipe (both PVC pipes run outside) or one-pipe (intake open in its closet)?

## 6c. Readiness schedule (agreed 2026-11-01)

Each gate has dated pass/fail criteria, computed from live data by `tools/dehum_readiness.py` (read-only, runs on
ha-2). `tools/dehum_readiness_report.sh` files it daily on .210 at 06:41 PT (visko crontab, 14:41 UTC) under
`instance/dehum-study/reports/`: `<date>.md` + `latest.md`, `status.json` (gate status, machine-readable) and
`status-history.jsonl` (one line per day). So a missed review date doesn't lose track: the status shows what's met,
what's behind, and what's waiting on Hugh. An open Claude session also reviews it daily (session cron).

| Gate | Due (PT) | Pass criteria | Unlocks |
|---|---|---|---|
| **G2a** HVAC meters zeroed | Tue Nov 3 | HVAC pair (`study_hvac_return`, `study_hvac_supply`) registered + reporting. Zero offset from ≥ 6 h of blower-on / Aprilaire-idle minutes over ≥ 2 days, SE ≤ 0.05 g/m³ (heat cycles give ~2.5 h a day of these, in short stretches). Fan tap and furnace venting known (Hugh). Midea pair offset from ≥ 12 idle h (met 2026-11-01: −0.40 ± 0.06 g/m³) | Across-handler measurement of the Aprilaire |
| **G2b** Phase 2 done | Sun Nov 8 | ≥ 5 Aprilaire runs (one run = one call; the E070 cycles its compressor off ~4 min an hour) measured across the air handler. Half-range ≤ ±20 % of the median L/kWh. All runs 0.3–2.5 L/kWh | Build the rotation scheduler; Midea plug back on for its arms; **Hugh's go-ahead to rotate** |
| **P3** Rotation coverage | Sun Nov 15, then weekly (22, 29, Dec 6) | Each arm ≥ 8 valid blocks, baseline ≥ 10. Each unit ≥ 2 inlet dew-point bins (< 7.5, 7.5–9.5, ≥ 9.5 °C) with ≥ 4 blocks. Each unit's L/kWh 95 % CI within ±15 %. The scheduler writes control.db `study_block` (arm, inlet_dp_c, l_per_kwh, end_reason) | Phase 4: fit surfaces, control law in shadow |

Expected Phase 4 start ≈ Nov 29 – Dec 6; the limiter is weather variety (at least one mild damp spell), not block
count. This winter's fit covers indoor dew points of only ~7–10 °C; the humid end waits for summer.

Not used as a G2b criterion: an in-place airflow check *across the whole air handler*. The Aprilaire's sensible
rise and latent term nearly cancel once diluted in ~1,000 cfm (cp·ΔT ≈ 1.4 kJ/kg vs h_fg·ΔW ≈ 1.2 kJ/kg), so the
airflow estimate is ill-conditioned. A meter in the E070's own discharge (`study_apr_discharge`, optional) would
make it well-conditioned and give ~5× the ΔAH signal.

## 7. Decisions

**Settled (Hugh, 2026-10-31):**
- **No hard-coded targets.** The end product is a **UI** that shows the user the efficiency and energy impact of
  their settings: water removed, kWh, and L/kWh at the current conditions. The user picks the trade-off. The
  control law exposes these as preferences rather than fixed thresholds (supersedes the target wording in §5).
- **Automatic rotation (D) is approved.** Design details are under discussion (2026-10-31).
- **Pricing:** fixed rate today, not time-of-use. Cost is a later, separate "energy → dollars" layer on top of
  kWh; the study itself is in kWh and litres.
- **Power metering is already complete for the study:** Midea = `plug_hall`, Aprilaire = `dehum_pm`, ERV =
  `erv_pm`. Hugh's spare plug meters aren't needed here. Method B needs **temperature/RH** meters, not power
  meters.

**Open:**
- Study meter intake as separate study devices (§3D, with Hugh).
- Rotation (§3D): Hugh to review the full proposal. Blower = follows Aprilaire; 24 h; upper guard 47–48 % are settled.
