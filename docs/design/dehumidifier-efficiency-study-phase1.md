# Dehumidifier study — Phase 1 first pass: whole-house moisture balance (2026-10-31)

Parent: [dehumidifier-efficiency-study.md](dehumidifier-efficiency-study.md) §3A. Tool: `tools/dehum_phase1.py`
(hourly OLS on ha-2 rollups; room set + local outdoor from the [Phase 0 audit](dehumidifier-efficiency-study-phase0.md)).

## Finding: the house air is a small part of the house's moisture store, so an air-only balance can't measure removal

- **The fit is weak and the removal terms are ~25× too small.** R² 0.14. Midea 0.04 L/kWh against **0.97
  L/kWh from the bucket**. ERV and crawl coefficients have implausible signs.
- **Arithmetic:** the Midea removes ≈ 450 g/h. If only the 349 m³ of room air held that water, indoor AH would fall
  ≈ 1.3 g/m³ every hour it runs. Observed hourly changes are ~0.1 g/m³.
- **The rest comes out of storage.** Wood, drywall, furnishings, and the crawl and attic exchange release
  stored water as the air dries (*moisture buffering*). Matching the fit to the bucket implies an effective
  moisture capacity ≈ **26× the air volume**, inside the range reported for furnished houses.
- **Confounding:** the units run *because* the house is humid (hysteresis on living-room RH), and the ERV mode,
  weather and occupancy move together. An observational hourly regression can't separate them.
- **Relative result, weak:** Aprilaire ≈ 0.46 × Midea per kWh (±~60 %). With the Midea at 0.97 L/kWh that's
  ~0.45 L/kWh, but not to be trusted yet.

## Consequences for the plan

1. **Per-unit efficiency comes from methods B (across-unit meters + in-place airflow calibration) and C
   (Midea bucket), not A.** Install the HVAC return/supply pair (2026-11-01) and keep the Midea pair.
2. **Method A becomes the *house* model, not the unit meter:** a two-node model (room air + an effective
   material buffer) fitted with the per-unit removal *known* from B/C. That model is what the control law needs:
   how fast the house dew point responds to an hour of each lever, and how much stored water stands behind it.
3. **The designed rotation (D) matters more than planned.** Randomised on/off blocks remove the feedback
   confounding that sinks the observational fit.

## Raw output

### Phase 1 — whole-house moisture balance (2026-10-31 16:23 PDT)

Window 2026-10-05 → 2026-10-31 16:00 PT; V = 349 m3. Hours used 538; dropped {'event': 56, 'shower': 46, 'missing': 0}.

R² 0.137; residual SD 0.114 g/m3/h

| Term | Estimate | ± SE | Meaning |
|---|---|---|---|
| k_leak (1/h) | 0.1044 | 0.022 | leakage ≈ 21 cfm-equivalent (0.10 ACH) |
| k_erv (1/h per 100 cfm) | -0.08282 | 0.025 | ERV: -17% of its airflow's moisture difference crosses (1 − latent recovery) |
| k_crawl (1/h) | -0.03344 | 0.012 | crawl coupling ≈ -7 cfm-equivalent |
| source day (g/m3/h) | 0.1042 | 0.015 | 36 g/h net daytime source |
| source night (g/m3/h) | 0.06079 | 0.019 | 21 g/h net night source |
| Aprilaire (g per kWh) | 17.23 | 10 | **0.02 L/kWh** |
| Midea (g per kWh) | 37.57 | 8.4 | **0.04 L/kWh** |

Hours with Aprilaire > 150 Wh: 167; with Midea > 150 Wh: 307. Rated: Midea IEF 2.01 L/kWh (65 °F/60 %); E070 1.8–2.1 L/kWh (73–80 °F/60 %). Bucket method (Sep–Oct): Midea 0.97 L/kWh.
