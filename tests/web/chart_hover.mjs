// Exercise the chart hover readout's pure math (server/web/app.js AdaptiveChart) — the part that has
// real edge cases (binary search, px↔time round-trip under a stretched viewBox, the staleness gate).
// Driven by tests/test_chart_hover.py so it runs inside the normal suite; also runnable bare:
//   node tests/web/chart_hover.mjs
import { readFileSync } from "node:fs";
const APP = new URL("../../server/web/app.js", import.meta.url);
const src = readFileSync(APP, "utf8");

// pull the two new helpers straight out of the real file so this tests the shipped code, not a copy
const grab = (name) => {
  const i = src.indexOf(`function ${name}(`);
  if (i < 0) throw new Error(`missing ${name}`);
  let d = 0, j = src.indexOf("{", i);
  for (let k = j; k < src.length; k++) {
    if (src[k] === "{") d++; else if (src[k] === "}" && --d === 0) return src.slice(i, k + 1);
  }
};
const { nearestPoint, medianGap } = await import(
  "data:text/javascript," + encodeURIComponent(
    `${grab("nearestPoint")}\n${grab("medianGap")}\nexport { nearestPoint, medianGap };`));

let fails = 0;
const ok = (name, cond, extra = "") => { if (!cond) { fails++; console.log(`FAIL ${name} ${extra}`); }
  else console.log(`ok   ${name}`); };

// ── nearestPoint ────────────────────────────────────────────────────────────
const pts = [0, 10, 20, 30, 40].map((t) => ({ t: t * 60000, v: t }));
ok("exact hit",        nearestPoint(pts, 20 * 60000).v === 20);
ok("before first",     nearestPoint(pts, -9e6).v === 0);
ok("after last",       nearestPoint(pts, 9e9).v === 40);
ok("rounds down",      nearestPoint(pts, 24 * 60000).v === 20, `got ${nearestPoint(pts, 24 * 60000).v}`);
ok("rounds up",        nearestPoint(pts, 26 * 60000).v === 30, `got ${nearestPoint(pts, 26 * 60000).v}`);
ok("exact midpoint→earlier", nearestPoint(pts, 25 * 60000).v === 20);
ok("two-point series", nearestPoint(pts.slice(0, 2), 11 * 60000).v === 10);

// ── medianGap ───────────────────────────────────────────────────────────────
ok("even cadence", medianGap(pts) === 600000, `got ${medianGap(pts)}`);
ok("outlier gap resisted",
   medianGap([0, 60, 120, 9999].map((t) => ({ t: t * 1000 }))) === 60000,
   `got ${medianGap([0, 60, 120, 9999].map((t) => ({ t: t * 1000 })))}`);
ok("two points", medianGap(pts.slice(0, 2)) === 600000);

// ── pixel round-trip: chart-space ⇄ screen px, as onMove/x() do it ───────────
const W = 320, H = 150, pad = 8;
const tMin = 0, tMax = 3600000;
const x = (t) => pad + ((t - tMin) / (tMax - tMin || 1)) * (W - 2 * pad);
for (const w of [280, 640, 1000]) {          // the svg stretches; the mapping must survive any width
  for (const frac of [0, 0.25, 0.5, 0.9, 1]) {
    const t0 = tMin + frac * (tMax - tMin);
    const dotPx = x(t0) * w / W;                                    // forward: time → px (render)
    const cx = Math.min(Math.max(dotPx * W / w, pad), W - pad);      // inverse: px → chart-space (onMove)
    const t1 = tMin + ((cx - pad) / (W - 2 * pad)) * (tMax - tMin);
    ok(`round-trip w=${w} frac=${frac}`, Math.abs(t1 - t0) < 1, `Δ=${t1 - t0}ms`);
  }
}
// off-plot pointer clamps into the data range rather than extrapolating past it
for (const px of [-50, 5000]) {
  const cx = Math.min(Math.max(px * W / 640, pad), W - pad);
  const t = tMin + ((cx - pad) / (W - 2 * pad)) * (tMax - tMin);
  ok(`clamp px=${px}`, t >= tMin && t <= tMax, `t=${t}`);
}

// ── tolerance gate: a trace that stops mid-window must drop out of the readout ─
const span = tMax - tMin;
const live = Array.from({ length: 61 }, (_, i) => ({ t: i * 60000, v: i }));   // 1-min, full hour
const died = live.slice(0, 21);                                                // stops at t=20min
const tolOf = (p) => Math.max(3 * medianGap(p), span * 0.005);
const reports = (p, t) => Math.abs(nearestPoint(p, t).t - t) <= tolOf(p);
ok("live trace reports at 50min", reports(live, 50 * 60000));
ok("dead trace reports at 21min", reports(died, 21 * 60000));
ok("dead trace SILENT at 50min", !reports(died, 50 * 60000));
const hourly = Array.from({ length: 25 }, (_, i) => ({ t: i * 3600000, v: i })); // weather cadence
ok("hourly trace still reports between samples", reports(hourly, 1800000));

console.log(fails ? `\n${fails} FAILURE(S)` : "\nall pass");
process.exit(fails ? 1 : 0);
