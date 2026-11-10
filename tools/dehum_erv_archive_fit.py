# dehum_erv_archive_fit.py — refit the HVAC pair's fan-only zero (offset + ERV term) on the FULL raw archive
# (parquet + hot.db), not the 7-day 1-min rungs, with stability checks: by ERV flow, by day, a duct-temperature term,
# and today's block run under each model (study doc §6b, 2026-11-09; Hugh: "an abundance of historical data").
# Library-mode on top of tools/dehum_readiness.py; needs duckdb (ha-2 venv):
#   { echo "_AS_LIBRARY = True"; cat tools/dehum_readiness.py tools/dehum_erv_archive_fit.py; } |
#     ssh 192.168.1.200 'cd home_automation && nice -n 15 venv/bin/python -'
import duckdb, sqlite3 as _sq, collections, statistics as S2
PQ = "instance/db/parquet/year=2026/month=1[01]/*.parquet"
_cache = {}
def _minute(dev, metric):
    if (dev, metric) in _cache: return _cache[(dev, metric)]
    acc = collections.defaultdict(list)
    for ts, v in duckdb.sql(f"select ts, value from read_parquet('{PQ}') where device_id='{dev}' and metric='{metric}' and ts>='2026-10-31'").fetchall():
        acc[ts[:16]].append(v)
    h = _sq.connect("file:instance/db/hot.db?mode=ro", uri=True)
    for ts, v in h.execute("select ts, value from readings where device_id=? and metric=? and ts>='2026-11-09'", (dev, metric)):
        acc[ts[:16]].append(v)
    out = {}
    for m, vs in acc.items():
        out[int(datetime.fromisoformat(m + ":00+00:00").timestamp())] = sum(vs) / len(vs)
    # forward-fill state-like series to 1-min so held() sees every minute (rungs did this by bucketing last values)
    _cache[(dev, metric)] = out
    return out
class PData(Data):
    def __init__(self): pass
    def series(self, dev, metric, a, b, res="1min"):
        s = _minute(dev, metric)
        if metric in ("running", "hvac_state", "active_stage", "power_w", "supply_cfm"):
            ks = sorted(s); out = {}; j = 0
            for k in range(a - a % 60, b, 60):
                while j + 1 < len(ks) and ks[j + 1] <= k: j += 1
                if ks and ks[j] <= k and k - ks[j] <= 900: out[k] = s[ks[j]]
            return out
        return {k: v for k, v in s.items() if a <= k < b}
    def first_seen(self, dev):
        s = _minute(dev, "humidity_pct"); return min(s) if s else None

d = PData()
now = int(datetime.now(timezone.utc).timestamp()) // 60 * 60
study_a = int(datetime(2026, 10, 31, 16, 30, tzinfo=PT).timestamp())
apr = d.series("dehum_pm", "power_w", study_a, now)
blower = d.series("hvac_blower", "running", study_a, now)
hstate = d.series("thermostat_hall", "hvac_state", study_a, now)
apr_idle = {k: v < APR_IDLE_W for k, v in apr.items()}
blower_on = {k: v >= 0.99 for k, v in blower.items()}
blower_off = {k: v <= 0.01 for k, v in blower.items()}
no_heat = {k: hstate.get(k, 0) < 0.5 for k in blower}
state = {}
for k in blower:
    if not (held(apr_idle, k, 15) and held(no_heat, k, 25)): continue
    if held(blower_on, k, 3): state[k] = "S1f"
erv = d.series("erv_attic", "supply_cfm", study_a, now)
w_ret = d.ah_series(HV_RET, study_a, now)
delta = pair_delta(d, HV_RET, HV_SUP, list(blower), study_a, now) or {}
def xdiff_from(w_out):
    ok, j, x = sorted(w_out), 0, {}
    for k in sorted(w_ret):
        while j + 1 < len(ok) and ok[j + 1] <= k: j += 1
        if ok and ok[j] <= k and k - ok[j] <= 900: x[k] = w_out[ok[j]] - w_ret[k]
    return x
X = {"cross-checked": xdiff_from(outdoor_ah(d, study_a - 1800, now)), "sensor only": xdiff_from(d.ah_series(OUTDOOR, study_a - 1800, now))}
t_ret, t_sup = d.series(HV_RET, "temperature_c", study_a, now), d.series(HV_SUP, "temperature_c", study_a, now)

def chunks(x):
    sub = sorted(k for k in delta if state.get(k) == "S1f" and k in x and k in erv)
    rows, cur, last = [], [], None
    for k in sub:
        if cur and (k - last > 60 or len(cur) >= 60): rows.append(cur); cur = []
        cur.append(k); last = k
    if cur: rows.append(cur)
    return [ks for ks in rows if len(ks) >= 5]
def fit(rows, x, extra=None):
    xs = [S.mean(erv[k] * x[k] / 100 for k in ks) for ks in rows]
    ys = [S.mean(delta[k] for k in ks) for ks in rows]
    A = [[1.0, xv] + ([extra(ks)] if extra else []) for ks, xv in zip(rows, xs)]
    f = ols(A, ys)
    return f, xs, ys
print(f"data {pt(study_a):%m-%d} -> {pt(now):%m-%d %H:%M} PT")
for name, x in X.items():
    rows = chunks(x); f, xs, ys = fit(rows, x)
    cf = [S.mean(erv[k] for k in ks) for ks in rows]
    print(f"\n[{name}] S1f chunks {len(rows)} ({sum(map(len, rows))/60:.1f} h); ERV cfm {min(cf):.0f}-{max(cf):.0f}; cfm*x/100 {min(xs):+.2f}..{max(xs):+.2f}")
    print(f"  all: offset {f[0][0]:+.3f} ± {f[1][0]:.3f}  g {f[0][1]:+.3f} ± {f[1][1]:.3f}")
    if name != "cross-checked": continue
    for lab, sel in (("ERV <= 80 cfm", lambda c: c <= 80), ("ERV 80-110", lambda c: 80 < c <= 110), ("ERV > 110", lambda c: c > 110)):
        rr = [ks for ks, c in zip(rows, cf) if sel(c)]
        if len(rr) >= 3:
            g2 = fit(rr, x)[0]; print(f"  {lab:14} n {len(rr):3}  offset {g2[0][0]:+.3f} ± {g2[1][0]:.3f}  g {g2[0][1]:+.3f} ± {g2[1][1]:.3f}")
        else: print(f"  {lab:14} n {len(rr)}")
    days = sorted({pt(ks[0]).date() for ks in rows})
    print("  by day (offset with g held at the pooled fit; then free g):")
    for dd in days:
        rr = [ks for ks in rows if pt(ks[0]).date() == dd]
        res = [y - f[0][1] * xv for ks, xv, y in zip(rows, xs, ys) if pt(ks[0]).date() == dd]
        free = fit(rr, x)[0] if len(rr) >= 4 else None
        print(f"   {dd:%m-%d} n {len(rr):3}  offset@g {S.mean(res):+.3f}" + (f"   free: offset {free[0][0]:+.3f} g {free[0][1]:+.3f} ± {free[1][1]:.3f}" if free and free[1][1] is not None else ""))
    # does a duct temperature difference explain the drift? (sensor offset vs temperature)
    dT = lambda ks: S.mean(t_sup.get(k, 0) - t_ret.get(k, 0) for k in ks)
    tr = lambda ks: S.mean(t_ret.get(k, 0) for k in ks)
    for lab, ex in (("+ supply-return ΔT", dT), ("+ return temperature", tr)):
        f3 = fit(rows, x, ex)[0]
        r_all = [y - (f[0][0] + f[0][1]*xv) for xv, y in zip(xs, ys)]
        r3 = [y - (f3[0][0] + f3[0][1]*xv + f3[0][2]*ex(ks)) for ks, xv, y in zip(rows, xs, ys)]
        print(f"  {lab}: g {f3[0][1]:+.3f} ± {f3[1][1]:.3f}, coef {f3[0][2]:+.3f} ± {f3[1][2]:.3f}; resid SD {S.pstdev(r_all):.3f} -> {S.pstdev(r3):.3f}")

# today's block run under each model
x = X["cross-checked"]; rows = chunks(x)
s = int(datetime(2026, 11, 9, 12, 24, tzinfo=PT).timestamp()); e = max(k for k, v in apr.items() if v > APR_ON_W)
mins = [k for k in range(s, e, 60) if k in delta and k in x and k in erv and blower.get(k, 0) >= 0.99]
v = S.mean(delta[k] for k in mins); X1 = S.mean(erv[k] * x[k] / 100 for k in mins); T1 = S.mean(t_ret[k] for k in mins)
kwh = sum(apr.get(k, 0) for k in range(s - 1200, e, 60)) / 60000
q = TAP_CFM[1][1] * CFM
grams = lambda c: -c * q * len(mins) / 60
L = lambda c: grams(c) / 1000 / (kwh * len(mins) / ((e - s + 1200) / 60))
f = fit(rows, x)[0]; ft = fit(rows, x, lambda ks: S.mean(t_ret[k] for k in ks))[0]
trs = [S.mean(t_ret[k] for k in ks) for ks in rows]
print(f"\nrun {pt(s):%H:%M}-{pt(e):%H:%M}: raw ΔAH {v:+.3f}, cfm*x/100 {X1:+.2f}, return {T1:.1f} °C (fan-only chunks {min(trs):.1f}-{max(trs):.1f} °C), {kwh:.2f} kWh")
for lab, c in (("pooled offset + g", v - f[0][0] - f[0][1] * X1),
               ("+ return-temperature term", v - ft[0][0] - ft[0][1] * X1 - ft[0][2] * T1),
               ("no ERV term (g 0)", v - f[0][0])):
    print(f"  {lab:28} ΔAH {c:+.3f} -> {L(c):.2f} L/kWh")
