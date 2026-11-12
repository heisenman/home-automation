# Unified control law — temperature, humidity, air quality across every actuator

**Status (2026-11-12): L1–L2 built and running in shadow on ha-2** (Hugh: "implement L1-L3 and let's run them a
few days collecting data"). L3 fits in progress. Nothing commands a device; the law logs what it would do in
control.db `law_shadow` every 5 min, next to what the rules did. Switch-over (L4) is per device, with Hugh's
approval. Code: `server/control/law/` (`bands`, `models`, `solve`, `shadow`); API `GET /api/v1/law`, `PUT
/api/v1/control/house/law`; PWA "⚖ Comfort" (top bar). Runbook Entry 4c.

**Origin (Hugh, 2026-11-12):** the house can be controlled for temperature and humidity, with the systems already
installed, by one unified law. Air quality dials in later; until then a rough rule applies: worse air drives the ERV
and the purifiers higher. "The law is an algorithm and the critical inputs are user-selected bands for
temperature, RH, AQ (the last being more fuzzy and fungible)." "This should eventually go into the PWA."

It generalises the dehumidifier study's Phase 4 law ([dehumidifier-efficiency-study.md §5](dehumidifier-efficiency-study.md))
from water alone to all three quantities, and absorbs the ERV dew-point law (`server/control/dewpoint_law.py`) as
the ventilator's part of it.

## 1. Principles

1. **An algorithm with user settings, not a tuned controller.** The user sets the bands. The algorithm holds the
   house inside them at the least cost. Nothing house-specific is hard-coded: the actuators, their models and the
   bands are all data.
2. **Bands are the inputs.**
   - Temperature and RH bands are **firm**. Leaving one is a violation the law must act on, cheapest lever first.
   - The AQ band is **soft and fungible**. Its weight lets it give way when a firm band needs the same actuator
     (for example, the ERV off for a wet night while the air is only mildly worse).
3. **Actuators are found by function, not by name.** The law gathers its levers from control.yaml's `function`
   tag (`server/control/functions.py`): `condition_temperature`, `dehumidify`, `ventilate`, `circulate_air`,
   `purify_air`, and later `shade`. A unit added later (an AC, a second ERV) joins without code changes once it
   has a function tag and a model.
4. **Each actuator carries a model of its effect, with a confidence.** Missing or weak model: that device keeps
   its current simple rule. So the law improves as the data fills in, without new code paths.
5. **Doubt pushes nowhere.** A stale or contradictory input zeroes its term (the dew-point law's rule, kept).
6. **Precedence is unchanged.** Safety interlocks, operator overrides, scenes, schedules and grid shed
   (ADR-0037) all beat the law, exactly as they beat today's rules.
7. **Shadow first, then one device at a time** (study §5). The PWA shows the shadow decision next to the actual
   one until Hugh switches a device over.

## 2. Inputs (user settings)

| Setting | Shape | Default (today's behaviour) |
|---|---|---|
| Temperature band | low/high °F, house-wide or per room, optional day/night | the thermostat's own setpoints (the comfort ranges in control.yaml: heat 62–74 °F) |
| RH band | low/high %, house-wide or per room | the dehumidifiers' current thresholds (48/45 Aprilaire, 46/43 Midea) |
| AQ band | a floor on the unified AQ index (ADR-0035, 0–100, lower = worse), plus a weight | the ERV's live bands (≤ 60 → high, ≤ 75 → med) |
| Cost basis | `kwh` or `money` (electric $/kWh, gas $/therm) | `kwh` |
| Per-device enrolment | `rule` (today's policy) / `shadow` / `law` | `shadow` |

- **RH or dew point?** The user sets RH, because that's what people feel and read. Internally the law controls
  the water content that RH implies: dew point at the band's temperature. RH then doesn't swing just because the
  temperature moved (study §5). In winter the RH ceiling is also limited by window condensation, which the law
  reports as a warning when the user's band would cross it.
- Settings live in control.db `house_setting` (key `law:bands`, `law:cost`), reusing `control_store`'s JSON
  key/value. No new table for settings.

## 3. Actuator models

One model per actuator kind, chosen by `function` + `device_type`. A model answers, for the current conditions
(indoor T/RH, outdoor T/RH, AQ, time of day):

- **effect** per hour at each of its levels: Δ house temperature, Δ house water (g/h), Δ AQ;
- **cost** per hour at each level: kWh electric, therms gas;
- **limits and interlocks:** minimum on/off times, needs-the-blower, cold limits, safe states;
- **confidence:** `good` / `rough` / `none`, plus where the numbers came from.

The current inventory:

| Actuator | Temperature | Water | AQ | Confidence today | Source / gap |
|---|---|---|---|---|---|
| Furnace (`condition_temperature`) | heat | small drying | **a VOC source**: closet VOC +119 at 10 min when the blower starts with the call (16 calls), +53 when it's already running (28) | **none** for heat | gap: a heat-loss model (UA, thermal mass), fittable from thermostat runs + outdoor T, data on file |
| Blower (`circulate_air`) | mixes | required by the Aprilaire | spreads ERV/purifier air | rough | rated tap airflow, 150 W assumed (not metered) |
| Aprilaire (`dehumidify`) | +0.6–0.8 kW into the supply | ~0.7 L/kWh at dew point 7.5–9.5 °C | — | rough | study G2b; method-B evening dips unexplained (study §6b 11-12) |
| Midea (`dehumidify`) | +0.48 kW, one room | ~1.04 L/kWh (bucket) | — | good (efficiency), one room | study 11-10 bucket |
| ERV (`ventilate`) | heat recovery | moisture exchange ∝ cfm × outdoor−indoor AH (fitted 0.035–0.055 per 100 cfm) | the only fresh-air source | rough (water), none (temperature, AQ) | gaps: sensible recovery (`supply_temp_c`, which air stream it reads is unverified); AQ response (the off/min/high nights 11-10 → 11-12); the ERV's own cold schedule |
| Purifiers (`purify_air`) | — | — | particles only; the VOC-based AQ index barely sees them | none | gap: is the Levoit PM2.5 populated? (living-room unit was stuck at 983) |
| Shades (`shade`) | solar gain | — | — | none | not installed |

**Heat is not waste in winter.** A dehumidifier's heat replaces furnace heat while the house is calling for it,
so its net cost is its kWh minus the gas it saves. That makes the ranking depend on the cost basis. In summer
(or with the house above its temperature band) the same heat is a penalty.

## 4. The algorithm (one tick)

1. **Read** the house: per-room T/RH, house dew point, AQ, outdoor (sensor cross-checked with Open-Meteo), each
   input's age. Stale input → its term is 0.
2. **Errors:** for each band, how far outside it (0 inside), scaled by the band's width. A firm band is an error
   to clear. The soft AQ band is a weighted error.
3. **Candidates:** for each enrolled actuator, its levels' predicted effects and costs from its model, within its
   limits and interlocks (Aprilaire on ⇒ blower on; ERV inside its cold limit; minimum on/off times).
4. **Choose** the cheapest set of levels that clears the firm errors within a horizon (the house buffers water
   ~26× its air volume, so the humidity horizon is hours, not minutes), then spend on the AQ error by its
   weight. Small, greedy, explainable: the actuators are few, so try combinations rather than optimise
   generally.
5. **Hysteresis and holds:** same rule as the dew-point law. A bigger move in the same direction is taken at
   its edge. A smaller or reversing one only once the inputs, nudged back, no longer support the current one.
   Then the minimum hold.
6. **Explain:** every decision carries the errors, the levers considered with their cost per unit, and the
   reason the winner won. This goes to control_log and the PWA, the way the dew-point law reports its terms.

**The rough AQ rule (until the AQ model exists):** AQ below its band raises the ERV and the purifiers one level
per `aq_step`. The ERV half is the dew-point law's AQ force, which already exists. **Furnace link:** a heat call
is a predicted VOC rise, larger when the blower starts with the call, so the law may give the ERV and purifiers a
short pre-emptive push on the call. Off by default. Whether the rooms see what the closet sees still needs
checking.

## 5. Modules (decomposition before code, Principle 3)

| Module | Job | Reuses |
|---|---|---|
| `server/control/law/bands.py` | settings schema, defaults from today's policies, per-room/day-night resolution, validation | `control_store` `house_setting` |
| `server/control/law/models.py` | the actuator model registry (by function + device_type): effect, cost, limits, confidence | `functions.py`; study fits (constants first, refit later); `dry_boost.conditions` for outdoor cross-check |
| `server/control/law/solve.py` | **pure** tick: (house state, bands, models, current levels) → plan + explanation | `dewpoint_law` (the ventilator part; its forces become the ERV model's effect) |
| `server/control/law/shadow.py` | runs the solver every tick, logs plan vs actual into control.db `law_shadow` | the AQ v2 shadow pattern |
| controller hook | for devices enrolled `law`, the plan replaces the rule's output at the same point the dew-point law hooks in today, below overrides/scenes/schedules | `controller._apply_dewpoint_law` placement |
| `server/api/control.py` + viewmodel | GET/PUT the bands/cost/enrolment, expose the shadow view | existing policy validation + `vm.controls` (ADR-0013: author once in the BFF) |
| PWA: "House comfort" panel | bands (sliders, °F, per room optional), cost basis, per-device rule/shadow/law, each actuator's model confidence, today's shadow vs actual with the explanations | `RangedSettings` patterns in `server/web/app.js` |

## 6. Stages

| Stage | What changes live | Needs Hugh |
|---|---|---|
| L0 | this design | review |
| L1 | settings + API + PWA panel; defaults mirror today's rules, so nothing changes | — |
| L2 | models from the existing fits + solver + shadow log | — |
| L3 | fill the gaps from data on file: house heat-loss fit, ERV AQ response (the three ERV nights), ERV sensible recovery, furnace VOC reach into the rooms, purifier PM | — |
| L4 | per-device switch-over from the PWA, ERV first (it's already law-shaped) | approve each device |

The study's Phase 3 rotation still runs underneath: it's what turns the dehumidifier models from rough to good.

### As built (L1–L2, 2026-11-12)

- **Bands** (`law:bands`): temperature °F on the five-room mean, RH on `meter_pro_living_room` (the meter both
  dehumidifiers run on), AQ floor 75 / weight 1 kWh-per-missing-level / a level per 10 points, horizon 3 h.
  Defaults: 68–76 °F, 40–46 % RH. House-wide only for now; per-room and day/night bands come later.
- **Cost** (`law:cost`): `kwh` (electricity only, the default) or `money` (electric $/kWh + gas $/therm; heat
  credited against the furnace's gas while heating, charged at AC COP 3 while cooling).
- **Water need:** the excess over the band's top × the house's buffered volume (520 m³ × 26) ÷ horizon; once
  drying, to the band's middle (today's on-high/off-part-way hysteresis). Below the band: no drying.
- **Search:** every combination of the dehumidifiers (off/on) × the ERV's levels, ranked by (firm shortfall, cost
  + AQ shortfall × weight, changes). Purifiers by the rough AQ rule. The thermostat gets the band as its setpoints.
- **Shadow:** 15-min hold on the law's own answers; enrolment `rule` leaves a device out of the comparison.
- **Models** (`server/control/law/models.py`): Midea 480 W at 1.04 L/kWh (good); Aprilaire 580 W + blower 150 W
  at 0.75 (rough); ERV 0/66/92/125 cfm, latent pass-through 0.3 (rough), sensible recovery unknown; furnace and
  house heat: none. L3's fits land in `house_setting` `law:params` with the same keys.

## 7. Open questions

- Cooling: **the house has a single-stage AC** (ADR-0043). The model inventory has no AC model yet; summer needs one.
- Per-room bands vs one house band: rooms differ by ~2–3 °F on cold nights with one thermostat. The law can
  only steer what its actuators reach (the furnace is whole-house; the Midea is one room).
- The ERV's own cold behaviour: on 11-12 07:01 PST, in LOW at ~51 °F intake, it stopped both fans (0 cfm, 7.7 W)
  for the first time in 14 days of LOW. If that's Broan's intermittent schedule, the ERV model must include it,
  and the law's cold force stacks on it.
