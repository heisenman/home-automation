#!/usr/bin/env python3
"""aq_experiment_report.py — daily report on the air-quality v2 SHADOW experiment (ADR-0035 amendment
2026-10-10; plan + findings in docs/air-quality/SENSOR-METHODOLOGY.md §9).

Runs ON the system of record (ha-2, where the gas data is): read-only, stdlib + duckdb only (no pandas — the
repo venv is shared). Prints a markdown report to stdout. Scheduled from .210 by
systemd/ha-aq-experiment-report.{service,timer}, which ssh-runs it and files the output under
instance/aq-experiment/reports/ — so the experiment is tracked by code, independent of any Claude session.

What it measures (each section answers a question the experiment exists to settle):
  Flags       — anything worth raising with Hugh (the top of the report; empty = nothing new)
  Health      — is v2 actually being written, per node, at the expected ~1/min?
  Bands       — v1 vs v2 band share per gas node (did v2 un-saturate the BME680 rooms?)
  Rooms       — per-area worst-of: gas v1 vs gas v2 ⊕ PM2.5 (Phase 3: how often particles set the band)
  Context     — Phase 2: room-only vs house-wide episodes, and how often a room is unusual for the hour
  Ventilation — Phase 2b: humidity "accumulating / closed off" share by hour + flagged nights per room meter
  Head-to-head— areas with two gas families (mech_closet): band agreement + event hit-rate, v1 vs v2
  Diurnal     — median v2 score by local hour (the house-wide afternoon dip — real or artifact?)
  Drift       — BME680 gas resistance growth/week; SGP30 TVOC daily medians
  Refit       — per-node absolute-humidity coefficient vs the shared code constant
  Events      — NOx > 20, PM2.5 > 35, Sensirion VOC Index > 300 (house-wide vs room-only)

Usage (on ha-2):  venv/bin/python tools/aq_experiment_report.py [--days 7]
"""
import argparse
import math
import sqlite3
import statistics as st
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from server.room_ventilation import COUPLING_THR, EXCESS_THR, ventilation_state  # noqa: E402
from server.gas_compensation import (AQ_HOUSE_POOR, AQ_LOCAL_THR, AQ_UNUSUAL_THR, BME_V2_K_AH,  # noqa: E402
                                     PM25_WINDOW_S, absolute_humidity_gm3, area_worst, band_for_score,
                                     house_median, pm25_air_quality)

HOT = REPO / "instance/db/hot.db"
PARQUET = REPO / "instance/db/parquet"
TZ = ZoneInfo("America/Los_Angeles")
BIN_S = 300
BANDS = ["Excellent", "Good", "Fair", "Poor", "Very Poor"]
GAS_FAMS = ("sgp30", "sgp40", "sgp41", "bme680")
METRICS = ("air_quality", "air_quality_v2", "air_quality_pm25", "aq_local_excess", "aq_hour_anomaly", "vent_coupling", "vent_ah_excess", "gas_ohm", "temperature_c", "humidity_pct", "voc_index",
           "nox_index", "tvoc", "pm25_ugm3")


def family(dtype):
    dt = (dtype or "").lower()
    return next((f for f in ("sgp30", "sgp41", "sgp40", "bme680") if f in dt), None)


def load(since_ep):
    """{(device_id, metric): {bin_ep: mean}} at 5-min resolution, parquet archive + today's hot.db."""
    since_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since_ep))
    out = defaultdict(dict)
    mlist = ",".join(f"'{m}'" for m in METRICS)
    files = sorted(PARQUET.glob("year=*/month=*/*.parquet"))
    if files:
        import duckdb
        c = duckdb.connect()
        flist = ",".join(f"'{f}'" for f in files)
        for b, d, m, v in c.execute(
                f"SELECT CAST(floor(epoch(strptime(ts, '%Y-%m-%dT%H:%M:%SZ')) / {BIN_S}) * {BIN_S} AS BIGINT) b, "
                f"device_id, metric, avg(value) FROM read_parquet([{flist}]) "
                f"WHERE ts >= ? AND metric IN ({mlist}) GROUP BY ALL", [since_iso]).fetchall():
            out[(d, m)][int(b)] = v
    h = sqlite3.connect(f"file:{HOT}?mode=ro", uri=True)
    for b, d, m, v in h.execute(
            f"SELECT (CAST(strftime('%s', ts) AS INTEGER) / {BIN_S}) * {BIN_S}, device_id, metric, avg(value) "
            f"FROM readings WHERE ts >= ? AND authoritative=1 AND metric IN ({mlist}) GROUP BY 1, 2, 3",
            (since_iso,)):
        out[(d, m)][int(b)] = v
    devs = {d: (t, a) for d, t, a in h.execute("SELECT device_id, device_type, area FROM device_last_seen")}
    v2_rows = {(d, m): n for d, m, n in h.execute(
        "SELECT device_id, metric, count(*) FROM readings WHERE metric IN ('air_quality_v2','air_quality_pm25') "
        "AND ts >= ? GROUP BY 1, 2", (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400)),))}
    h.close()
    return out, devs, v2_rows


def band(score):
    return band_for_score(score)[1]


def band_share(series):
    if not series:
        return None
    n = len(series)
    counts = defaultdict(int)
    for v in series.values():
        counts[band(v)] += 1
    return {b: 100.0 * counts[b] / n for b in BANDS}


def fmt_share(sh):
    short = {"Excellent": "Exc", "Good": "Good", "Fair": "Fair", "Poor": "Poor", "Very Poor": "VPoor"}
    return " · ".join(f"{short[b]} {sh[b]:.0f}%" for b in BANDS if sh[b] >= 0.5) if sh else "—"


def pearson(xs, ys):
    if len(xs) < 10:
        return None
    try:
        return st.correlation(xs, ys)
    except st.StatisticsError:
        return None


def rolling_detrend(rows, since, window_s):
    """rows = [(ep, y, x)] sorted by ep → (ys, xs) for ep ≥ since, each minus its trailing-window median."""
    import bisect
    win_y, win_x, q = [], [], []
    ys, xs = [], []
    j = 0
    for k, y, x in rows:
        bisect.insort(win_y, y)
        bisect.insort(win_x, x)
        q.append((y, x))
        while rows[j][0] < k - window_s:
            oy, ox = q.pop(0)
            win_y.pop(bisect.bisect_left(win_y, oy))
            win_x.pop(bisect.bisect_left(win_x, ox))
            j += 1
        if k >= since:
            ys.append(y - win_y[len(win_y) // 2])
            xs.append(x - win_x[len(win_x) // 2])
    return ys, xs


def local_hour(ep):
    return datetime.fromtimestamp(ep, TZ).hour


def local_day(ep):
    return datetime.fromtimestamp(ep, TZ).date()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7, help="analysis window (default 7)")
    a = ap.parse_args()
    now = time.time()
    since = now - a.days * 86400
    data, devs, v2_rows = load(since - 2 * 86400)          # +2 d lead-in for detrending
    S = lambda d, m: {b: v for b, v in data.get((d, m), {}).items() if b >= since}   # noqa: E731
    gas = sorted((d, family(t), ar) for d, (t, ar) in devs.items() if family(t))
    flags, out = [], []
    P = out.append

    # ── Health ──
    P("## Health — v2 rows written, last 24 h (expect ~1440/node at 1/min)\n")
    P("| node | family | area | v2 rows | v1 rows (5-min bins) |\n|---|---|---|---|---|")
    for d, f, ar in gas:
        n2 = v2_rows.get((d, "air_quality_v2"), 0)
        n1 = sum(1 for b in data.get((d, "air_quality"), {}) if b >= now - 86400)
        P(f"| {d} | {f} | {ar} | {n2} | {n1} |")
        if n1 > 100 and n2 < 1000:
            flags.append(f"**{d}**: only {n2} v2 rows in 24 h (v1 is live) — v2 sampler gap or BME burn-in.")

    pm_devs = sorted({d for (d, m) in data if m == "pm25_ugm3"})
    for d in pm_devs:
        n = v2_rows.get((d, "air_quality_pm25"), 0)
        P(f"| {d} | PM2.5 | {devs.get(d, ('', '?'))[1]} | {n} (air_quality_pm25) | — |")
        if n < 500:
            flags.append(f"**{d}**: only {n} air_quality_pm25 rows in 24 h — PM sampler gap or sparse purifier feed.")

    # ── Bands ──
    P(f"\n## Band share, last {a.days} d — v1 (live) vs v2 (shadow)\n")
    P("| node | v1 | v2 |\n|---|---|---|")
    for d, f, ar in gas:
        s1, s2 = band_share(S(d, "air_quality")), band_share(S(d, "air_quality_v2"))
        P(f"| {d} ({f}) | {fmt_share(s1)} | {fmt_share(s2)} |")

    # ── Rooms: worst-of per area (Phase 3) ──
    # PM is REPLAYED from raw pm25 (trailing 1 h of 5-min bins, the sampler's formula) so the comparison has
    # history from day one; the stored air_quality_pm25 series is checked under Health.
    P(f"\n## Rooms — worst-of per area, last {a.days} d: gas v1 (live) vs gas v2 ⊕ PM2.5 (shadow)\n")
    P("| area | sensors | bins | room v1 | room v2 ⊕ PM | bins PM set the band |\n|---|---|---|---|---|---|")
    pm_score = {}
    for d in pm_devs:
        raw = data[(d, "pm25_ugm3")]
        ks = sorted(raw)
        sc = {}
        for i, k in enumerate(ks):
            win = [raw[x] for x in ks[max(0, i - 12):i + 1] if x > k - PM25_WINDOW_S]
            r = pm25_air_quality(st.mean(win), len(win))["air_quality"] if win else None
            if r is not None and k >= since:
                sc[k] = r
        pm_score[d] = sc
    areas = sorted({ar for _, _, ar in gas} | {devs[d][1] for d in pm_devs if d in devs})
    for ar in areas:
        g_ids = [d for d, f, a2 in gas if a2 == ar]
        p_ids = [d for d in pm_devs if devs.get(d, (None, None))[1] == ar]
        r1, r2, pm_won = {}, {}, 0
        for d in g_ids:
            for k, v in S(d, "air_quality").items():
                r1[k] = min(v, r1.get(k, 101))
            for k, v in S(d, "air_quality_v2").items():
                r2[k] = min(v, r2.get(k, 101))
        # like-for-like: a room with gas sensors is scored only on bins where ALL its gas nodes have v2
        # (and v1 on those same bins); a PM-only room on its PM bins
        if g_ids:
            keep = set.intersection(*(set(S(d, "air_quality_v2")) for d in g_ids))
            r1 = {k: v for k, v in r1.items() if k in keep}
            r2 = {k: v for k, v in r2.items() if k in keep}
        for d in p_ids:
            for k, v in pm_score[d].items():
                if g_ids and k not in r2:
                    continue
                if v < r2.get(k, 101):
                    if k in r2 and band(v) != band(r2[k]):
                        pm_won += 1
                    r2[k] = v
        P(f"| {ar} | {', '.join(g_ids + p_ids)} | {len(r2)} | {fmt_share(band_share(r1))} | "
          f"{fmt_share(band_share(r2))} | {pm_won} |")

    # ── Context (Phase 2) ──
    fmt_t = lambda k: datetime.fromtimestamp(k, TZ).strftime("%a %m-%d %H:%M")   # noqa: E731

    def runs(series, pred, min_bins):
        ks = sorted(k for k, v in series.items() if pred(v))
        eps, cur = [], []
        for k in ks:
            if cur and k - cur[-1] > BIN_S:
                eps.append(cur)
                cur = []
            cur.append(k)
        if cur:
            eps.append(cur)
        return [e for e in eps if len(e) >= min_bins]

    P(f"\n## Context (Phase 2), last {a.days} d — room-only = local_excess ≤ {AQ_LOCAL_THR:.0f} for ≥ 15 min; "
      f"unusual = hour_anomaly ≤ {AQ_UNUSUAL_THR:.0f}\n")
    P("| node | bins | local_excess p10 / med | room-only episodes (start hours) | unusual-for-hour share |")
    P("|---|---|---|---|---|")
    recent = []
    for d, f, ar in gas:
        le, ha = S(d, "aq_local_excess"), S(d, "aq_hour_anomaly")
        if not le and not ha:
            P(f"| {d} | 0 | — (history still banking) | — | — |")
            continue
        vals = sorted(le.values())
        eps = runs(le, lambda v: v <= AQ_LOCAL_THR, 3)
        hrs = defaultdict(int)
        for e in eps:
            hrs[local_hour(e[0])] += 1
            recent.append((e[0], e[-1], d, min(le[x] for x in e)))
        unusual = 100.0 * sum(v <= AQ_UNUSUAL_THR for v in ha.values()) / len(ha) if ha else None
        P(f"| {d} | {len(le)} | {vals[len(vals) // 10]:.0f} / {st.median(vals):.0f} | {len(eps)} "
          f"({', '.join(f'{h:02d}h×{n}' for h, n in sorted(hrs.items()))}) | "
          f"{'—' if unusual is None else f'{unusual:.0f}%'} |")
    if recent:
        P("\nMost recent room-only episodes:")
        for s0, s1, d, mn in sorted(recent)[-8:]:
            P(f"- {fmt_t(s0)}–{fmt_t(s1)[-5:]} **{d}** (down to {mn:.0f} pts vs house)")
    # house-wide: the median of per-area worst v2, per 5-min bin
    node_area = {d: ar for d, f, ar in gas}
    v2 = {d: S(d, "air_quality_v2") for d, f, ar in gas}
    hb = {k: house_median(area_worst({d: v2[d].get(k) for d in v2}, node_area))
          for k in sorted({k for x in v2.values() for k in x})}
    hb = {k: v for k, v in hb.items() if v is not None}
    heps = runs(hb, lambda v: v < AQ_HOUSE_POOR, 6)
    hh = defaultdict(int)
    for e in heps:
        hh[local_hour(e[0])] += 1
    P(f"\nHouse-wide episodes (house median < {AQ_HOUSE_POOR:.0f} for ≥ 30 min): {len(heps)} "
      f"({', '.join(f'{h:02d}h×{n}' for h, n in sorted(hh.items())) or '—'})")

    # ── Ventilation (Phase 2b) ──
    P(f"\n## Ventilation (Phase 2b), last {a.days} d — accumulating = mixing r < {COUPLING_THR} AND moisture "
      f"build-up > +{EXCESS_THR} g/m³\n")
    P("Ground truth (Hugh, 2026-10-10): c_bed door SHUT every night (Catherine); h_office door OPEN (Hugh); "
      "h_bed meter sits in the closet. Shadow-tune: c_bed 43/68 nights flagged ≥ 1 h (00–07 h), h_office 19/68.\n")
    P("| meter | area | accumulating share by hour (00/04/08/12/16/20) | nights flagged ≥ 1 h (00–07 h) | now |")
    P("|---|---|---|---|---|")
    vent_devs = sorted({d for (d, m) in data if m == "vent_coupling"})
    for d in vent_devs:
        cp, ex = S(d, "vent_coupling"), S(d, "vent_ah_excess")
        flag = {k: ventilation_state(cp[k], ex[k])["state"] == "accumulating" for k in cp if k in ex}
        if not flag:
            continue
        byh = defaultdict(list)
        nights = defaultdict(int)
        for k, f in flag.items():
            byh[local_hour(k) // 4 * 4].append(f)
            if local_hour(k) < 7:
                nights[local_day(k)] += f
        shares = " / ".join(f"{100.0 * sum(byh[h]) / len(byh[h]):.0f}%" if byh[h] else "—" for h in range(0, 24, 4))
        last = max(flag)
        P(f"| {d} | {devs.get(d, ('', '?'))[1]} | {shares} | {sum(1 for n in nights.values() if n >= 12)}/{len(nights)} "
          f"| {'accumulating' if flag[last] else 'mixed'} |")
    if not vent_devs:
        P("- no ventilation data yet")

    # ── Head-to-head: any area with ≥2 gas families ──
    by_area = defaultdict(list)
    for d, f, ar in gas:
        by_area[ar].append((d, f))
    for ar, nodes in by_area.items():
        bme = [d for d, f in nodes if f == "bme680"]
        sgp = [d for d, f in nodes if f in ("sgp40", "sgp41")]
        if not (bme and sgp):
            continue
        b, s = bme[0], sgp[0]
        P(f"\n## Head-to-head — {ar}: {b} (BME680) vs {s} (Sensirion), last {a.days} d\n")
        ref = S(s, "air_quality")
        vi = S(s, "voc_index")
        P("| BME series | bins | band agreement | pearson | event hit-rate (VOC Idx>200 → BME<80) | false alarms (BME<60, VOC Idx<150) |")
        P("|---|---|---|---|---|---|")
        res = {}
        for name in ("air_quality", "air_quality_v2"):
            bs = S(b, name)
            common = sorted(set(bs) & set(ref))
            if not common:
                P(f"| {name} | 0 | — | — | — | — |")
                continue
            agree = 100.0 * sum(band(bs[k]) == band(ref[k]) for k in common) / len(common)
            r = pearson([bs[k] for k in common], [ref[k] for k in common])
            ev = [k for k in common if vi.get(k, 0) > 200]
            hit = 100.0 * sum(bs[k] < 80 for k in ev) / len(ev) if ev else None
            fa = [k for k in common if bs[k] < 60]
            fa_pct = 100.0 * sum(vi.get(k, 999) < 150 for k in fa) / len(fa) if fa else None
            res[name] = agree
            P(f"| {name} | {len(common)} | {agree:.0f}% | {'—' if r is None else f'{r:.2f}'} | "
              f"{'—' if hit is None else f'{hit:.0f}% of {len(ev)}'} | {'—' if fa_pct is None else f'{fa_pct:.0f}% of {len(fa)}'} |")
        if "air_quality_v2" in res and "air_quality" in res:
            P(f"\nPhase-1 target: band agreement ≥ 75 % (baseline 2026-10-10: v1 53 %). v2 − v1 = "
              f"{res['air_quality_v2'] - res['air_quality']:+.0f} pts.")
            if res["air_quality_v2"] >= 75:
                flags.append(f"**{ar}**: v2 band agreement {res['air_quality_v2']:.0f}% ≥ 75 % target — "
                             f"candidate to promote v2.")

    # ── Diurnal ──
    P(f"\n## Diurnal — median score by local hour, last {a.days} d (v2; Sensirion = v1 carried)\n")
    P("| node | " + " | ".join(f"{h:02d}" for h in range(0, 24, 2)) + " |")
    P("|---|" + "---|" * 12)
    for d, f, ar in gas:
        s = S(d, "air_quality_v2") or S(d, "air_quality")
        byh = defaultdict(list)
        for k, v in s.items():
            byh[local_hour(k) // 2 * 2].append(v)
        P(f"| {d} | " + " | ".join(f"{st.median(byh[h]):.0f}" if byh[h] else "—" for h in range(0, 24, 2)) + " |")

    # ── Drift ──
    P("\n## Drift — daily median raw signal\n")
    for d, f, ar in gas:
        metric = {"bme680": "gas_ohm", "sgp30": "tvoc"}.get(f)
        if not metric:
            continue
        byd = defaultdict(list)
        for k, v in S(d, metric).items():
            byd[local_day(k)].append(v)
        days = sorted(byd)
        if len(days) < 2:
            continue
        meds = [st.median(byd[x]) for x in days]
        unit = "kΩ" if metric == "gas_ohm" else "ppb"
        scale = 1000.0 if metric == "gas_ohm" else 1.0
        wk = (math.exp(math.log(meds[-1] / meds[0]) * 7 / max(1, (days[-1] - days[0]).days)) - 1) * 100 \
            if meds[0] > 0 and meds[-1] > 0 else 0.0
        P(f"- **{d}** {metric} ({unit}): " + ", ".join(f"{x:%m-%d} {m / scale:.0f}" for x, m in zip(days, meds))
          + f" — {wk:+.0f}%/week")
        if metric == "gas_ohm" and abs(wk) < 3:
            flags.append(f"**{d}**: gas resistance drift has flattened ({wk:+.1f}%/week) — BME conditioning may be done.")

    # ── Refit ──
    P(f"\n## Refit — absolute-humidity coefficient per BME680 (code constant k = {BME_V2_K_AH:.3f}/(g/m³))\n")
    for d, f, ar in gas:
        if f != "bme680":
            continue
        g, t, rh = (data.get((d, m), {}) for m in ("gas_ohm", "temperature_c", "humidity_pct"))
        rows = [(k, math.log(g[k]), absolute_humidity_gm3(t[k], rh[k])) for k in sorted(g)
                if k in t and k in rh and g[k] > 0]
        # detrend both by a trailing 48 h rolling median (removes the multi-week conditioning drift) — the
        # same method as the 2026-10-10 fit that set BME_V2_K_AH, so the comparison is like-for-like
        ys, xs = rolling_detrend(rows, since, 48 * 3600)
        if len(xs) < 100 or st.pvariance(xs) == 0:
            P(f"- {d}: not enough data")
            continue
        slope = st.covariance(xs, ys) / st.variance(xs)
        r2 = (pearson(xs, ys) or 0) ** 2
        P(f"- **{d}**: k = {-slope:.3f} (R² {r2 * 100:.0f}%, n={len(xs)})")
        if r2 > 0.15 and abs(-slope - BME_V2_K_AH) / BME_V2_K_AH > 0.4:
            flags.append(f"**{d}**: fitted humidity k {-slope:.3f} vs code {BME_V2_K_AH} (>40% off) — "
                         f"consider a per-node coefficient.")

    # ── Events ──
    P(f"\n## Events, last {a.days} d\n")
    def episodes(series, thr, min_bins=1):
        ks = sorted(k for k, v in series.items() if v > thr)
        eps, cur = [], []
        for k in ks:
            if cur and k - cur[-1] > 2 * BIN_S:
                eps.append(cur)
                cur = []
            cur.append(k)
        if cur:
            eps.append(cur)
        return [(e[0], e[-1], max(series[x] for x in e)) for e in eps if len(e) >= min_bins]
    any_ev = False
    for d, f, ar in gas:
        if f == "sgp41":
            for s0, s1, pk in episodes(S(d, "nox_index"), 20):
                any_ev = True
                P(f"- NOx **{d}** {fmt_t(s0)}–{fmt_t(s1)[-5:]} peak {pk:.0f}")
                flags.append(f"**{d}**: NOx Index event peak {pk:.0f} at {fmt_t(s0)} — combustion or restart transient?")
    for (d, m), ser in data.items():
        if m == "pm25_ugm3":
            for s0, s1, pk in episodes({k: v for k, v in ser.items() if k >= since}, 35):
                any_ev = True
                P(f"- PM2.5 **{d}** {fmt_t(s0)}–{fmt_t(s1)[-5:]} peak {pk:.0f} µg/m³ (gas AQ index does not see this)")
    sens = [d for d, f, ar in gas if f in ("sgp40", "sgp41")]
    high = defaultdict(set)
    for d in sens:
        for k, v in S(d, "voc_index").items():
            if v > 300:
                high[k].add(d)
    house = sum(1 for k, ds in high.items() if len(ds) >= max(2, len(sens) - 1))
    local = sum(1 for k, ds in high.items() if len(ds) == 1)
    P(f"- Sensirion VOC Index > 300: {house} house-wide bins (≥{max(2, len(sens) - 1)} rooms at once), "
      f"{local} single-room bins (5-min).")
    if not any_ev:
        P("- No NOx or PM2.5 episodes.")

    ts = datetime.now(TZ).strftime("%Y-%m-%d %H:%M %Z")
    print(f"# AQ v2 shadow experiment — {ts} (window {a.days} d)\n")
    print("## Flags\n")
    print("\n".join(f"- {x}" for x in flags) if flags else "- none")
    print()
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
