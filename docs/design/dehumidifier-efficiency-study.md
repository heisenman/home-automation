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
| House volume | `instance/house-geometry.json` (floor plan, 22 px/ft) | — | floor area from the plan; **ceiling height must be measured** |

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
furnace heat runs (kept — heating doesn't change the water content, only RH), and events Hugh labels.

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
  for the fan speed in use). That holds with heat on too, because heating doesn't change W.
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
| 0 | Data audit: pick per-room sensors, calibrate Open-Meteo vs local, measure ceiling height, collect the manual figures (airflow, rated capacity) | ceiling height; manuals |
| 1 | Method A on existing history (Midea Jun 24 →, Aprilaire Oct 6 →); first removal and efficiency estimates | — |
| 2 | Install inlet/outlet meters (method B); optional condensate spot checks (C) | sensors on hand? placement |
| 3 | Designed-experiment schedule (D), several weeks across weather | OK to rotate the units automatically |
| 4 | Fit surfaces → control law → shadow → switch over | approve switch-over |

**Already done:** Midea energy back-calculation (2026-10-31). The plug meter showed three clean levels: mode 2 =
compressor 464 W + 8.8 W/°C × (T − 21 °C); mode 1 = 1.4 W; and a ~35 W fan run-on. The control log matches the
reported mode 99 % of the time, and the estimate is within 1.5 % of the meter over Oct 19–30. Total: 811 kWh
(835 kWh with a 30 W allowance for high fan) from Jun 24 to Oct 31, about 6.2 kWh/day.

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
