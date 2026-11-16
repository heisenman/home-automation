# law_model_fit.py — Phase 4 formalization fits for the unified law (docs/design/unified-control-law-l3-fits.md §8):
#   A. the Midea's L/kWh against its inlet (living-room) dew point, from the scored rotation blocks;
#   B. Hugh's 2026-11-15 hypothesis: the Midea, near the return grille, feeds the Aprilaire drier air — the return
#      meter's AH relative to the house mean, and the Aprilaire's score, with and without the Midea;
#   C. the house-level response per unit (each unit's g/h, as the house actually sees it, from the house-mean AH
#      slope) with a both-running term, so the law stops treating the two as additive.
# Not standalone: it runs on ha-2 after tools/dehum_readiness.py loaded as a library, with .210's block scores:
#
#   { echo "_AS_LIBRARY = True"; echo "_PREV_OK = {}"; echo "_PREV_DONE = {}"; echo "_PREV_BLOCKS = {}";
#     echo "_PREV_ZERO = None"; cat tools/dehum_readiness.py;
#     echo "SC = $(sed 's/null/None/g' instance/dehum-study/reports/blocks.json)"; cat tools/law_model_fit.py; } |
#     ssh 192.168.1.200 'cd home_automation && nice python3 -'
#
# Prints the tables and, last, one JSON line `FIT {...}` (the law:params candidates).
# Forward check (Hugh 2026-11-15: fit now, compare against the next days): prepend `FIT_THROUGH = 54` and the fits
# use blocks ≤ 54 only; section D then predicts each later block's house-mean AH slope from its arm, unit power and
# ERV exchange, and prints predicted vs actual against the fit's residual SD.
import json as J
import statistics as S2
c = sqlite3.connect(f"file:{RUNGS}?mode=ro", uri=True); d = Data(c)
cc = sqlite3.connect(f"file:{CONTROL}?mode=ro", uri=True)
blocks = cc.execute("SELECT id, arm, start_ts, end_ts, inlet_dp_c FROM study_block WHERE end_reason='complete' "
                    "AND coalesce(note, '') NOT LIKE 'event:%' ORDER BY id").fetchall()
V, RET = 520.0, "study_hvac_return"


def slope(pts, t0):
    xs = [(k - t0) / 3600 for k, _ in pts]; ys = [v for _, v in pts]
    mx, my = S2.mean(xs), S2.mean(ys)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)


THROUGH = globals().get("FIT_THROUGH")
rows = []
for bid, arm, s0, e0, dp_in in blocks:
    ms, me = int(s0) // 60 * 60 + 30 * 60, int(e0) // 60 * 60
    per = [d.ah_series(r, ms, me) for r in ROOMS]
    keys = sorted(set.intersection(*[set(p) for p in per]))
    if len(keys) < 40:
        continue
    hm = {k: S2.mean(p[k] for p in per) for k in keys}
    ret = d.ah_series(RET, ms, me)
    rk = [k for k in keys if k in ret]
    ret_d = S2.mean(ret[k] - hm[k] for k in rk) if len(rk) >= 20 else None
    sc = SC.get(str(bid), {})

    def kw(dev):
        p = d.series(dev, "power_w", ms, me)
        return S2.mean(p.values()) / 1000 if p else 0.0
    km, ka = (kw("plug_hall") if arm in ("M", "MA") else 0.0), (kw("dehum_pm") if arm in ("A", "MA") else 0.0)
    r_m = (sc.get("midea") or 0.95) * km * 1000
    r_a = (sc.get("apr") or 0.77) * ka * 1000
    cfm = d.series("erv_attic", "supply_cfm", ms, me); wo = outdoor_ah(d, ms, me)
    x_erv = S2.mean(cfm.get(k, 0) * CFM * (wo[k] - v) for k, v in hm.items() if k in wo) if wo else 0.0
    rows.append(dict(id=bid, arm=arm, dp=dp_in, sl=slope(sorted(hm.items()), ms), ret=ret_d, km=km, ka=ka,
                     r_m=r_m, r_a=r_a, erv=x_erv, mid=sc.get("midea"), apr=sc.get("apr"), hour=pt(ms).hour))

later = [r for r in rows if THROUGH is not None and r["id"] > THROUGH]
rows = [r for r in rows if THROUGH is None or r["id"] <= THROUGH]
print("id arm  dp_in  dAH/dt  ret-house  midea_gph apr_gph  erv_x  L/kWh M / A")
for r in rows:
    print(f"{r['id']:>2} {r['arm']:<3} {r['dp'] or 0:5.1f} {r['sl']:+7.3f} "
          f"{'   —   ' if r['ret'] is None else format(r['ret'], '+7.3f')}  {r['r_m']:7.0f} {r['r_a']:7.0f} {r['erv']:7.0f}"
          f"  {r['mid'] or 0:.2f} / {r['apr'] or 0:.2f}")
fit = {}

# A. Midea L/kWh vs inlet dew point
pm = [(r["dp"], r["mid"]) for r in rows if r["mid"] and r["dp"] is not None]
b, se, _, s2 = ols([[1.0, x - 9.0] for x, _ in pm], [y for _, y in pm])
for arm in ("M", "MA"):
    v = [r["mid"] for r in rows if r["arm"] == arm and r["mid"]]
    print(f"Midea in {arm}: {S2.mean(v):.3f} ± {S2.stdev(v) / len(v) ** 0.5:.3f} L/kWh (n {len(v)})")
print(f"A. Midea L/kWh = {b[0]:.3f} ± {se[0]:.3f} + {b[1]:+.3f} ± {se[1]:.3f}/°C × (dp − 9); resid SD {s2 ** 0.5:.3f};"
      f" n {len(pm)}, dp {min(x for x, _ in pm):.1f}–{max(x for x, _ in pm):.1f} °C")
fit["midea"] = {"l_per_kwh": round(b[0], 3), "l_per_kwh_dp": {"at_c": 9.0, "per_c": round(b[1], 3), "se": round(se[0], 3),
                "slope_se": round(se[1], 3), "lo_c": min(x for x, _ in pm), "hi_c": max(x for x, _ in pm), "n": len(pm)}}

# B. the return meter vs the house mean, by arm; the Aprilaire's score with and without the Midea
for arm in ("0", "M", "A", "MA", "E"):
    v = [r["ret"] for r in rows if r["arm"] == arm and r["ret"] is not None]
    if len(v) >= 2:
        print(f"B. return − house mean AH in {arm:<2}: {S2.mean(v):+.3f} ± {S2.stdev(v) / len(v) ** 0.5:.3f} g/m³ (n {len(v)})")
for arm in ("A", "MA"):
    v = [r["apr"] for r in rows if r["arm"] == arm and r["apr"]]
    print(f"B. Aprilaire L/kWh in {arm:<2}: {S2.mean(v):.3f} ± {S2.stdev(v) / len(v) ** 0.5:.3f} (n {len(v)})")

# C. house-mean slope on each unit's removal, a both-running term and the ERV exchange
X = [[1.0, r["r_m"], r["r_a"], r["r_m"] if r["arm"] == "MA" else 0.0, r["erv"]] for r in rows]
y = [r["sl"] for r in rows]
b, se, _, s2 = ols(X, y)
names = ["load", "midea", "aprilaire", "midea when both", "erv exchange"]
for n_, bb, ss in zip(names, b, se):
    print(f"C. {n_:<16} {bb:+.6f} ± {ss:.6f}")
buf_m = -1 / (V * b[1]) if b[1] < 0 else None
seen = lambda bb: bb / b[1] if b[1] else None          # g/h as the house sees it, per g/h the unit removes
print(f"C. house buffer ×{buf_m:.1f} (on the Midea's term); Aprilaire seen {seen(b[2]):.2f}× the Midea's per g/h;"
      f" the Midea when both run {1 + seen(b[3]):.2f}× its alone; ERV pass {-seen(b[4]):.2f}; resid SD {s2 ** 0.5:.3f};"
      f" n {len(y)}")
fit["house"] = {"buffer_x": round(buf_m, 2) if buf_m else None, "apr_seen": round(seen(b[2]), 2),
                "midea_seen_both": round(1 + seen(b[3]), 2), "erv_pass": round(-seen(b[4]), 2),
                "load_gph": round(-b[0] / b[1], 0) if b[1] else None, "resid_sd": round(s2 ** 0.5, 3), "n": len(y),
                "se": [round(x, 6) for x in se], "beta": [round(x, 6) for x in b]}

# E. the Aprilaire's L/kWh without the HVAC pair (its zero has drifted, §6b 11-14/16): the house-mean slope per kW of
# each unit, scaled by the Midea's L/kWh — the house sees the Aprilaire's kW at this rate, in Midea-equivalent water.
# All blocks (the fit window and later)
allr = rows + later
Xe = [[1.0, r["km"], r["ka"], r["km"] if r["arm"] == "MA" else 0.0, r["erv"]] for r in allr]
be, see, _, s2e = ols(Xe, [r["sl"] for r in allr])
mid_alone = S2.mean(r["mid"] for r in allr if r["arm"] == "M" and r["mid"])
ratio = be[2] / be[1]
rse = abs(ratio) * ((see[2] / be[2]) ** 2 + (see[1] / be[1]) ** 2) ** 0.5
print(f"E. per kW: Midea {be[1]:+.3f} ± {see[1]:.3f}, Aprilaire {be[2]:+.3f} ± {see[2]:.3f} g/m³/h; Aprilaire/Midea "
      f"{ratio:.2f} ± {rse:.2f} → Aprilaire house-seen ≈ {ratio * mid_alone:.2f} ± {rse * mid_alone:.2f} L/kWh "
      f"(Midea alone {mid_alone:.2f}); n {len(allr)}")
fit["aprilaire_room_side"] = {"l_per_kwh_seen": round(ratio * mid_alone, 2), "se": round(rse * mid_alone, 2), "n": len(allr)}

# D. forward check: the fit on blocks ≤ FIT_THROUGH, predicting each later block
if later:
    rs = []
    print(f"D. forward check, fit through block {THROUGH}: id arm  predicted  actual  (resid / fit SD)")
    for r in later:
        x = [1.0, r["r_m"], r["r_a"], r["r_m"] if r["arm"] == "MA" else 0.0, r["erv"]]
        pr = sum(a * bb for a, bb in zip(x, b))
        rs.append(r["sl"] - pr)
        print(f"D. {r['id']:>3} {r['arm']:<3} {pr:+.3f}  {r['sl']:+.3f}  ({(r['sl'] - pr) / s2 ** 0.5:+.1f})")
    rms = (sum(e * e for e in rs) / len(rs)) ** 0.5
    print(f"D. {len(rs)} blocks: mean resid {S2.mean(rs):+.3f}, RMS {rms:.3f} vs fit SD {s2 ** 0.5:.3f} g/m³/h")
    fit["forward"] = {"through": THROUGH, "n": len(rs), "mean": round(S2.mean(rs), 3), "rms": round(rms, 3)}
print("FIT " + J.dumps(fit))
