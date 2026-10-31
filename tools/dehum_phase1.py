#!/usr/bin/env python3
"""Phase 1 of the dehumidifier study: whole-house moisture balance (method A) on existing data.
docs/design/dehumidifier-efficiency-study.md §3A, room set and outdoor choice from the Phase 0 audit.

Hourly model (W = absolute humidity g/m3, house average of the Phase 0 room set):

    dW_in/dt = k_leak*(W_out - W_in) + k_erv*cfm/100*(W_out - W_in) + k_crawl*(W_crawl - W_in)
               + s_day + s_night - e_apr*kWh_apr/V - e_mid*kWh_mid/V

so e_apr / e_mid come out directly as grams of water removed per kWh (x V = house volume). Fitted by ordinary least
squares on the centred hourly difference. Hours inside labelled event windows, or with a bathroom shower pulse, are
dropped. Stdlib only:

    ssh 192.168.1.200 'cd ~/home_automation && python3 - --since 2026-10-05' < tools/dehum_phase1.py
"""
import argparse
import math
import sqlite3
import statistics as S
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
RUNGS = "instance/db/rungs.db"
V_M3 = 349.0                                    # 1,575 ft2 x 94 in (provisional, Phase 0)
ROOMS = ["meter_pro_living_room", "meter_kitchen", "meter_c_bed", "meter_pro_c_office", "meter_pro_h_bed"]
OUTDOOR, CRAWL = "switchbot_outdoor", "ometer_crawlspace"
BATHS = ["meter_h_bathroom", "meter_master_bath"]
# labelled windows to drop (PT): spray foam, waxing, the wet tests, plus a margin
EVENTS = [("2026-10-26 12:00", "2026-10-28 06:00"), ("2026-10-29 16:00", "2026-10-30 06:00")]
SHOWER_JUMP = 0.8                               # g/m3 rise in a bathroom within the hour vs the house = a shower


def ah(t, rh):
    return 6.112 * math.exp(17.67 * t / (t + 243.5)) * rh * 2.1674 / (273.15 + t)


def hourly(c, dev, metric, a, b):
    return dict(c.execute("SELECT bucket_start, vmean FROM rung WHERE res='1hour' AND device_id=? AND metric=? "
                          "AND bucket_start>=? AND bucket_start<?", (dev, metric, a, b)))


def ah_series(c, dev, a, b):
    t, h = hourly(c, dev, "temperature_c", a, b), hourly(c, dev, "humidity_pct", a, b)
    return {k: ah(t[k], h[k]) for k in t if k in h}


def pt_ep(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=PT).timestamp())


def ols(X, y):
    """Least squares with standard errors, by normal equations + Gauss-Jordan (no numpy on ha-2)."""
    n, p = len(X), len(X[0])
    A = [[sum(X[r][i] * X[r][j] for r in range(n)) for j in range(p)] for i in range(p)]
    bvec = [sum(X[r][i] * y[r] for r in range(n)) for i in range(p)]
    M = [A[i][:] + [1.0 if i == j else 0.0 for j in range(p)] for i in range(p)]
    for col in range(p):
        piv = max(range(col, p), key=lambda r: abs(M[r][col]))
        M[col], M[piv] = M[piv], M[col]
        d = M[col][col]
        if abs(d) < 1e-12:
            raise SystemExit(f"singular design (column {col}) — a regressor is constant or collinear")
        M[col] = [v / d for v in M[col]]
        for r in range(p):
            if r != col:
                f = M[r][col]
                M[r] = [vr - f * vc for vr, vc in zip(M[r], M[col])]
    inv = [row[p:] for row in M]
    beta = [sum(inv[i][j] * bvec[j] for j in range(p)) for i in range(p)]
    res = [y[r] - sum(beta[i] * X[r][i] for i in range(p)) for r in range(n)]
    s2 = sum(e * e for e in res) / max(n - p, 1)
    se = [math.sqrt(max(inv[i][i] * s2, 0)) for i in range(p)]
    ybar = S.mean(y)
    r2 = 1 - sum(e * e for e in res) / sum((v - ybar) ** 2 for v in y)
    return beta, se, r2, res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-10-05", help="PT date (local outdoor meter + Aprilaire meter start 10-04)")
    ap.add_argument("--until", default=None, help="PT 'YYYY-MM-DD HH:MM' (default now)")
    args = ap.parse_args()
    a = pt_ep(args.since + " 00:00")
    b = pt_ep(args.until) if args.until else int(datetime.now(timezone.utc).timestamp()) // 3600 * 3600
    c = sqlite3.connect(f"file:{RUNGS}?mode=ro", uri=True)

    rooms = [ah_series(c, d, a - 3600, b + 3600) for d in ROOMS]
    W = {k: S.mean(r[k] for r in rooms) for k in rooms[0] if all(k in r for r in rooms)}
    Wo, Wc = ah_series(c, OUTDOOR, a, b), ah_series(c, CRAWL, a, b)
    baths = [ah_series(c, d, a - 3600, b) for d in BATHS]
    cfm = hourly(c, "erv_attic", "supply_cfm", a, b)
    p_apr = hourly(c, "dehum_pm", "power_w", a, b)
    p_mid = hourly(c, "plug_hall", "power_w", a, b)
    mode = hourly(c, "dehumidifier_living_room", "mode", a, b)
    t_mid = hourly(c, "dehumidifier_living_room", "temperature_c", a, b)
    events = [(pt_ep(x), pt_ep(y)) for x, y in EVENTS]

    def midea_kwh(k):                           # metered from 10-18; before that the back-calculated model
        if k in p_mid:
            return p_mid[k] / 1000
        if k in mode and k in t_mid:
            frac = max(0.0, min(1.0, mode[k] - 1.0))   # hourly mean of mode 1/2 = share of the hour in Continuous
            return (frac * (464 + 8.8 * (t_mid[k] + 0.84 - 21)) + (1 - frac) * 1.4) / 1000
        return None

    X, y, keep, dropped = [], [], [], {"event": 0, "shower": 0, "missing": 0}
    for k in range(a, b, 3600):
        if any(x <= k < z for x, z in events):
            dropped["event"] += 1
            continue
        if any(k - 3600 in bs and k + 3600 in bs and k + 3600 in W and k - 3600 in W and
               (bs[k + 3600] - bs[k - 3600]) - (W[k + 3600] - W[k - 3600]) > SHOWER_JUMP for bs in baths):
            dropped["shower"] += 1
            continue
        km = midea_kwh(k)
        if not all(q in W for q in (k - 3600, k, k + 3600)) or k not in Wo or k not in Wc or k not in cfm \
                or k not in p_apr or km is None:
            dropped["missing"] += 1
            continue
        dWdt = (W[k + 3600] - W[k - 3600]) / 2          # g/m3 per hour, centred
        do, dc = Wo[k] - W[k], Wc[k] - W[k]
        night = 1.0 if datetime.fromtimestamp(k, PT).hour in (22, 23, 0, 1, 2, 3, 4, 5, 6) else 0.0
        X.append([do, cfm[k] / 100 * do, dc, 1.0 - night, night, -p_apr[k] / 1000 / V_M3, -km / V_M3])
        y.append(dWdt)
        keep.append(k)

    names = ["k_leak (1/h)", "k_erv (1/h per 100 cfm)", "k_crawl (1/h)", "source day (g/m3/h)",
             "source night (g/m3/h)", "Aprilaire (g per kWh)", "Midea (g per kWh)"]
    print(f"# Phase 1 — whole-house moisture balance ({datetime.now(PT):%Y-%m-%d %H:%M %Z})\n")
    print(f"Window {datetime.fromtimestamp(a, PT):%Y-%m-%d} → {datetime.fromtimestamp(b, PT):%Y-%m-%d %H:%M} PT; "
          f"V = {V_M3:.0f} m3. Hours used {len(y)}; dropped {dropped}.\n")
    beta, se, r2, res = ols(X, y)
    print(f"R² {r2:.3f}; residual SD {S.pstdev(res):.3f} g/m3/h\n")
    print("| Term | Estimate | ± SE | Meaning |\n|---|---|---|---|")
    meaning = [lambda v: f"leakage ≈ {v * V_M3 / 1.699:.0f} cfm-equivalent ({v:.2f} ACH)",
               lambda v: f"ERV: {v * V_M3 / 169.9 * 100:.0f}% of its airflow's moisture difference crosses "
                         "(1 − latent recovery)",
               lambda v: f"crawl coupling ≈ {v * V_M3 / 1.699:.0f} cfm-equivalent",
               lambda v: f"{v * V_M3:.0f} g/h net daytime source", lambda v: f"{v * V_M3:.0f} g/h net night source",
               lambda v: f"**{v / 1000:.2f} L/kWh**", lambda v: f"**{v / 1000:.2f} L/kWh**"]
    for nm, bt, s, f in zip(names, beta, se, meaning):
        print(f"| {nm} | {bt:.4g} | {s:.2g} | {f(bt)} |")
    ka = sum(1 for r in X if r[5] < -0.15 / V_M3)   # hours with > 150 Wh of Aprilaire
    km = sum(1 for r in X if r[6] < -0.15 / V_M3)
    print(f"\nHours with Aprilaire > 150 Wh: {ka}; with Midea > 150 Wh: {km}. "
          f"Rated: Midea IEF 2.01 L/kWh (65 °F/60 %); E070 1.8–2.1 L/kWh (73–80 °F/60 %). "
          "Bucket method (Sep–Oct): Midea 0.97 L/kWh.")


if __name__ == "__main__":
    main()
