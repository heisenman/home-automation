# law_buffer_fit.py — fit the house's short-term water buffer (and the ERV's latent pass-through) from the study's
# rotation blocks, for the unified law's `law:params` house.buffer_x (docs/design/unified-control-law-l3-fits.md §6).
# Not standalone: it runs on ha-2 after tools/dehum_readiness.py loaded as a library, with the block scores from .210:
#
#   { echo "_AS_LIBRARY = True"; echo "_PREV_OK = {}"; echo "_PREV_DONE = {}"; echo "_PREV_BLOCKS = {}";
#     cat tools/dehum_readiness.py;
#     echo "SC = $(sed 's/null/None/g' instance/dehum-study/reports/blocks.json)"; cat tools/law_buffer_fit.py; } |
#     ssh 192.168.1.200 'cd home_automation && nice python3 -'
#
# Model, per completed block (first 30 min dropped): d(house-mean AH)/dt = (load − removal + a·ERV exchange) / (V·k).
# OLS of the slope on [1, removal g/h, ERV exchange g/h at full pass-through] gives k = −1/(V·b1), a = −b2/b1.
import statistics as S2
c = sqlite3.connect(f"file:{RUNGS}?mode=ro", uri=True); d = Data(c)
cc = sqlite3.connect(f"file:{CONTROL}?mode=ro", uri=True)
blocks = cc.execute("SELECT id, arm, start_ts, end_ts, end_reason FROM study_block WHERE end_reason='complete' "
                    "ORDER BY id").fetchall()
V = 520.0
prev_rem = {}
rows = []
for bid, arm, s0, e0, _ in blocks:
    ms, me = int(s0) // 60 * 60 + 30 * 60, int(e0) // 60 * 60
    per = [d.ah_series(r, ms, me) for r in ROOMS]
    keys = sorted(set.intersection(*[set(p) for p in per]))
    if len(keys) < 40:
        continue
    hm = [(k, S2.mean(p[k] for p in per)) for k in keys]
    lr = d.ah_series(LR, ms, me)
    def slope(pts):
        xs = [(k - ms) / 3600 for k, _ in pts]; ys = [v for _, v in pts]
        mx, my = S2.mean(xs), S2.mean(ys)
        return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    sl_h, sl_lr = slope(hm), slope(sorted(lr.items()))
    sc = SC.get(str(bid), {})
    def rem(dev, key, arms, default):
        if arm not in arms: return 0.0
        p = d.series(dev, "power_w", ms, me); kw = S2.mean(p.values()) / 1000 if p else 0
        return (sc.get(key) or default) * kw * 1000          # g/h
    r_m = rem("plug_hall", "midea", ("M", "MA"), 0.93)
    r_a = rem("dehum_pm", "apr", ("A", "MA"), 0.68)
    cfm = d.series("erv_attic", "supply_cfm", ms, me); wo = outdoor_ah(d, ms, me)
    x_erv = S2.mean(cfm.get(k, 0) * CFM * (wo[k] - v) for k, v in hm if k in wo)   # g/h if latent pass were 1
    rows.append((bid, arm, sl_h, sl_lr, r_m, r_a, x_erv, pt(ms).hour))
print("id arm  dAH/dt house  LR   midea_gph apr_gph  erv_x  hour")
for r in rows: print(f"{r[0]:>2} {r[1]:<3} {r[2]:+7.3f} {r[3]:+7.3f} {r[4]:7.0f} {r[5]:7.0f} {r[6]:7.0f}  {r[7]:02d}")
for label, idx in (("house mean", 2), ("living room", 3)):
    X = [[1.0, r[4] + r[5], r[6]] for r in rows]; y = [r[idx] for r in rows]
    b, se, _, s2 = ols(X, y)
    k = -1 / (V * b[1]) if b[1] < 0 else float("nan")
    print(f"{label}: load {b[0]*V:+.0f} g/h-air-equiv; dAH/dt per g/h removed {b[1]:+.5f} ± {se[1]:.5f} -> buffer x{k:.1f}"
          f"; ERV pass {b[2]/b[1]*-1 if b[1] else 0:+.2f} (as fraction of removal-equivalent); resid SD {s2**0.5:.3f} g/m3/h; n={len(y)}")
