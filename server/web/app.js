// Home Automation — web app (Preact + HTM, no build step). The vendored runtime IS the dependency;
// this file is plain readable JS you can edit on the box and reload. Talks to the API/BFF we serve.
import {
  html, render, useState, useEffect, useRef, useCallback, useMemo, createContext, useContext,
} from "/app/vendor/preact-htm.standalone.module.js";

// ── units (°C/°F) ────────────────────────────────────────────────────────────
// Temperatures are STORED in Celsius (SI). The UI converts for display only. Preference persisted.
const UnitsCtx = createContext("F");
const useTemp = () => useContext(UnitsCtx);
const tempPref = () => localStorage.getItem("ha.tempUnit") || "F";
const isTempMetric = (m) => m === "temperature_c";
const convT = (c, unit) => (unit === "F" ? c * 9 / 5 + 32 : c);
const tUnit = (unit) => (unit === "F" ? "°F" : "°C");

// ── admin token ──────────────────────────────────────────────────────────────
// The control endpoints want Authorization: Bearer SHA256("ha-api:"+master). We derive that hash in the
// browser (Web Crypto) and store ONLY the derived token — never the master — so a peek at localStorage
// can't recover the passphrase. Read-only views need no token.
const TOKEN_KEY = "ha.adminToken";
const getToken = () => localStorage.getItem(TOKEN_KEY) || "";
const setToken = (t) => (t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY));

async function deriveToken(master) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode("ha-api:" + master));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// ── api helpers ──────────────────────────────────────────────────────────────
async function getJSON(path) {
  const r = await fetch(path, { cache: "no-store" });
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json();
}
async function adminSend(method, path, body) {
  const r = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", Authorization: "Bearer " + getToken() },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.reason || data.detail || `HTTP ${r.status}`);
  return data;
}

// ── small format helpers ─────────────────────────────────────────────────────
function fmtAge(s) {
  if (s == null) return "—";
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  return `${(s / 3600).toFixed(1)}h ago`;
}
const round1 = (v) => (v == null ? "—" : (Math.round(v * 10) / 10));
// axis time label: clock for ≤2 days, else M/D
function fmtClock(ms, spanH) {
  const d = new Date(ms), p = (n) => String(n).padStart(2, "0");
  return spanH <= 48 ? `${p(d.getHours())}:${p(d.getMinutes())}` : `${d.getMonth() + 1}/${d.getDate()}`;
}
const range = (a, b) => Array.from({ length: b - a + 1 }, (_, i) => a + i);
const steps = (a, b, step) => { const o = []; for (let v = a; v <= b; v += step) o.push(v); return o; };
const isoNoMs = (d) => d.toISOString().replace(/\.\d+Z$/, "Z");

// pull a metric's time-series for a device over an explicit ISO window (bounded/downsampled server-side).
async function fetchReadingsRange(deviceId, metric, startISO, endISO, limit = 500) {
  const q = `start=${startISO}&end=${endISO}&metric=${metric}&limit=${limit}`;
  const d = await getJSON(`/devices/${encodeURIComponent(deviceId)}/readings?${q}`);
  return (d.readings || []).map((r) => ({ t: Date.parse(r.ts), v: r.value })).filter((p) => !isNaN(p.t));
}

// distinct line colors for overlaying multiple sources on one chart
const PALETTE = ["#4aa3ff", "#34d399", "#fbbf24", "#f87171", "#a78bfa", "#22d3ee", "#fb923c", "#f472b6"];

// bump on each UI change — shown in the header so we can confirm at a glance which build a client loaded.
const BUILD = "v15 (2026-06-23)";

// fetch one trace's series (a sensor metric OR a weather metric) over an ISO window → [{t,v}].
async function fetchTrace(tr, startISO, endISO) {
  if (tr.kind === "weather") {
    const q = `metric=${tr.metric}&start=${startISO}&end=${endISO}&location=${encodeURIComponent(tr.source)}`;
    const d = await getJSON(`/weather/readings?${q}`);
    return (d.readings || []).map((r) => ({ t: Date.parse(r.ts), v: r.value })).filter((p) => !isNaN(p.t));
  }
  return fetchReadingsRange(tr.source, tr.metric, startISO, endISO);
}

// the catalog of selectable traces = every sensor metric + every weather metric.
function traceCatalog(sensors, weather) {
  const out = [];
  for (const s of (sensors || [])) {
    for (const g of GRAPHABLE) {
      if (s.metrics[g.key] != null) {
        out.push({ key: `s:${s.device_id}:${g.key}`, kind: "sensor", source: s.device_id,
                   metric: g.key, label: `${prettyName(s.device_id)} · ${g.label}`, unit: g.unit });
      }
    }
  }
  if (weather && weather.available) {
    for (const loc of (weather.locations || [])) {
      for (const m of (weather.metrics || [])) {
        const g = GRAPHABLE.find((x) => x.key === m) || { label: m, unit: "" };
        out.push({ key: `w:${loc}:${m}`, kind: "weather", source: loc, metric: m,
                   label: `weather ${loc} · ${g.label}`, unit: g.unit });
      }
    }
  }
  return out;
}

// apply the temperature unit to a series for display (storage is always °C).
function prepSeries(s, unit) {
  if (isTempMetric(s.metric) && unit === "F") {
    return { ...s, points: s.points.map((p) => ({ t: p.t, v: convT(p.v, "F") })), unit: "°F" };
  }
  return s;
}

// ── override controls ────────────────────────────────────────────────────────
function OverrideControls({ id, override, isAdmin, onChange, onNeedAdmin }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const act = async (action, duration_min) => {
    if (!isAdmin) return onNeedAdmin();
    setBusy(true); setErr("");
    try {
      await adminSend("POST", `/control/${id}/override`, { action, duration_min });
      await onChange();
    } catch (e) { setErr(String(e.message)); }
    setBusy(false);
  };
  const left = override && override.expires_in_min != null ? Math.ceil(override.expires_in_min) : null;
  return html`
    <div class="controls">
      <button class="btn sm" disabled=${busy} onClick=${() => act("off", 60)}>Off 1h</button>
      <button class="btn sm" disabled=${busy} onClick=${() => act("boost_on", 60)}>Boost 1h</button>
      ${override && html`<button class="btn sm ghost" disabled=${busy}
          onClick=${() => act("clear")}>Resume auto</button>`}
      ${override && html`<span class="note">override: <b>${override.action}</b>${
          left != null ? ` · ${left}m left` : ""}</span>`}
      ${err && html`<span class="err">${err}</span>`}
    </div>`;
}

// ── settings (app-mutable policy) ────────────────────────────────────────────
function SettingsPanel({ vm, sensors, isAdmin, onChange, onNeedAdmin }) {
  const c = vm.control || {};
  const [open, setOpen] = useState(false);
  const [enabled, setEnabled] = useState(!!c.enabled);
  const [source, setSource] = useState(vm.control.source_sensor || "");
  const [onAbove, setOnAbove] = useState(c.on_above ?? "");
  const [offBelow, setOffBelow] = useState(c.off_below ?? "");
  const [quiet, setQuiet] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [flash, setFlash] = useState("");

  // candidate sources = trusted sensors that actually report humidity. Keep the current source in the
  // list even if it's momentarily offline/absent, so saving never silently drops it.
  const opts = (sensors || [])
    .filter((s) => s.metrics && s.metrics.humidity_pct != null)
    .map((s) => ({ id: s.device_id, label: `${prettyName(s.device_id)} · ${prettyArea(s.area)}` }));
  if (source && !opts.some((o) => o.id === source)) {
    opts.unshift({ id: source, label: `${prettyName(source)} (current)` });
  }

  const save = async () => {
    if (!isAdmin) return onNeedAdmin();
    setBusy(true); setErr(""); setFlash("");
    const patch = {
      enabled,
      control: { strategy: c.strategy || "hysteresis", on_above: Number(onAbove), off_below: Number(offBelow) },
    };
    if (source) patch.source_sensor = source;
    if (quiet.trim()) patch.schedule = [{ when: quiet.trim(), policy: "off" }];
    try {
      await adminSend("PUT", `/control/${vm.device_id}/policy`, patch);
      setFlash("saved"); await onChange();
    } catch (e) { setErr(String(e.message)); }
    setBusy(false);
  };

  if (!open) {
    return html`<div class="settings"><button class="btn sm ghost"
        onClick=${() => setOpen(true)}>⚙ Settings</button></div>`;
  }
  return html`
    <div class="settings">
      <div class="divider"></div>
      <label class="switch">
        <input type="checkbox" checked=${enabled} onChange=${(e) => setEnabled(e.target.checked)} />
        Automation enabled
      </label>
      <div class="field"><label>Humidity source</label>
        <select value=${source} onChange=${(e) => setSource(e.target.value)}>
          ${opts.length === 0 && html`<option value="">(no humidity sensors)</option>`}
          ${opts.map((o) => html`<option value=${o.id}>${o.label}</option>`)}
        </select></div>
      <div class="field"><label>Turn ON at/above (%RH)</label>
        <input type="number" value=${onAbove} onInput=${(e) => setOnAbove(e.target.value)} /></div>
      <div class="field"><label>Turn OFF below (%RH)</label>
        <input type="number" value=${offBelow} onInput=${(e) => setOffBelow(e.target.value)} /></div>
      <div class="field"><label>Quiet window</label>
        <input type="text" placeholder="22:00-07:00 (optional)" value=${quiet}
          onInput=${(e) => setQuiet(e.target.value)} /></div>
      <div class="controls">
        <button class="btn sm primary" disabled=${busy} onClick=${save}>Save</button>
        <button class="btn sm ghost" disabled=${busy} onClick=${() => setOpen(false)}>Close</button>
        ${flash && html`<span class="ok-flash">${flash}</span>`}
        ${err && html`<span class="err">${err}</span>`}
      </div>
      <p class="note">strategy: ${c.strategy} · the device's own RH is never used for control</p>
    </div>`;
}

// ── manual control (direct command set) ─────────────────────────────────────
function ManualControl({ vm, isAdmin, onChange, onNeedAdmin }) {
  const traits = vm.traits || {};
  const act = vm.actuator || {};
  const sp = traits.setpoint, rg = traits.ranged;
  const [open, setOpen] = useState(false);
  const [target, setTarget] = useState(act.target_pct ?? (sp && sp.safe_value) ?? "");
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");

  if (!sp && !rg) return null;                          // device exposes no manual functions

  const cmd = async (trait, args, tag) => {
    if (!isAdmin) return onNeedAdmin();
    setBusy(tag); setErr("");
    try {
      await adminSend("POST", `/devices/${vm.device_id}/command`, { trait, action: "set", args });
      await onChange();
    } catch (e) { setErr(String(e.message)); }
    setBusy("");
  };

  if (!open) {
    return html`<div class="settings"><button class="btn sm ghost"
        onClick=${() => setOpen(true)}>🎛 Manual control</button></div>`;
  }
  return html`
    <div class="settings">
      <div class="divider"></div>
      <p class="note">Direct device commands. Power is automation-managed — use the override buttons
        above to force on/off.</p>
      ${sp && html`
        <div class="field"><label>Target humidity</label>
          <input type="number" min=${sp.min} max=${sp.max} value=${target}
            onInput=${(e) => setTarget(e.target.value)} />
          <button class="btn sm primary" disabled=${busy === "target"}
            onClick=${() => cmd("setpoint", { value: Number(target) }, "target")}>Set</button>
          <span class="note">${sp.min}–${sp.max}%${act.target_pct != null ? ` · now ${act.target_pct}%` : ""}</span>
        </div>`}
      ${rg && (() => {
        const vals = rg.step ? steps(rg.min, rg.max, rg.step) : range(rg.min, rg.max);
        const NAMES = { 2: ["Low", "High"], 3: ["Low", "Med", "High"] };
        const name = (v, i) => (NAMES[vals.length] ? NAMES[vals.length][i] : String(v));
        return html`
        <div class="field"><label>Fan speed</label>
          <div class="controls">
            ${vals.map((n, i) => html`
              <button class="btn sm ${act.fan_speed === n ? "primary" : ""}" disabled=${busy === "fan"}
                title=${n} onClick=${() => cmd("ranged", { level: n }, "fan")}>${name(n, i)}</button>`)}
          </div>
          ${act.fan_speed != null && html`<span class="note">now: ${act.fan_speed}</span>`}
        </div>`;
      })()}
      ${err && html`<span class="err">${err}</span>`}
      <div class="controls"><button class="btn sm ghost" onClick=${() => setOpen(false)}>Close</button></div>
    </div>`;
}

// ── device card ──────────────────────────────────────────────────────────────
function DeviceCard({ vm, sensors, isAdmin, onChange, onNeedAdmin, onEdit }) {
  const running = vm.running;
  const s = vm.sensor, o = vm.onboard, d = vm.last_decision;
  const ageStale = vm.health === "stale";   // the BFF already derives staleness; mirror it on the age
  return html`
    <div class="card health-${vm.health}">
      <div class="card-head">
        <h2>${dispName(vm)}</h2>
        ${vm.room && html`<span class="sensor-area">${vm.room}</span>`}
        <span class="badge health-${vm.health}">${vm.health}</span>
        <button class="btn sm ghost edit-btn" onClick=${() => onEdit(vm)}>✎</button>
      </div>
      <div class="state-row">
        <span class="pill ${running ? "on" : "off"}">${running == null ? "?" : running ? "RUNNING" : "IDLE"}</span>
        ${vm.control.enabled === false && html`<span class="note">automation off</span>`}
      </div>
      <div class="readings">
        <div class="reading">
          <div class="v">${s ? round1(s.humidity_pct) + "%" : "—"}</div>
          <div class="k">control RH · <span class=${ageStale ? "age-stale" : "age-fresh"}>${
            s ? fmtAge(s.age_s) : "no data"}</span></div>
          <div class="k">from ${vm.control.source_sensor || "—"}</div>
        </div>
        <div class="reading muted">
          <div class="v">${o ? round1(o.humidity_pct) + "%" : "—"}</div>
          <div class="k">onboard (not used)</div>
        </div>
        <div class="reading muted">
          <div class="v">${vm.control.on_above ?? "—"}/${vm.control.off_below ?? "—"}</div>
          <div class="k">on/off thresholds</div>
        </div>
      </div>
      ${d && html`<div class="reason">${d.source}: ${d.reason}</div>`}
      <${OverrideControls} id=${vm.device_id} override=${vm.override} isAdmin=${isAdmin}
        onChange=${onChange} onNeedAdmin=${onNeedAdmin} />
      <${ManualControl} vm=${vm} isAdmin=${isAdmin} onChange=${onChange} onNeedAdmin=${onNeedAdmin} />
      <${SettingsPanel} vm=${vm} sensors=${sensors} isAdmin=${isAdmin}
        onChange=${onChange} onNeedAdmin=${onNeedAdmin} />
    </div>`;
}

// ── sensors (read-only) ──────────────────────────────────────────────────────
const prettyArea = (a) => (a || "unknown").replace(/_/g, " ");
const prettyName = (id) => id.replace(/^meter_/, "").replace(/_/g, " ");
// R8: prefer the user overlay (name/room), fall back to the prettified registry id/area.
const dispName = (o) => (o && o.name) || prettyName(o.device_id);
const dispRoom = (o) => (o && o.room) || prettyArea(o && o.area);

// which metrics get a graph, with display unit + line color
const GRAPHABLE = [
  { key: "temperature_c", unit: "°C", color: "#f87171", label: "Temperature" },
  { key: "humidity_pct", unit: "%RH", color: "#4aa3ff", label: "Humidity" },
  { key: "co2_ppm", unit: "ppm", color: "#fbbf24", label: "CO₂" },
  { key: "radon_bqm3", unit: "Bq", color: "#a78bfa", label: "Radon" },
  { key: "pressure_hpa", unit: "hPa", color: "#34d399", label: "Pressure" },
];

// shared value row (temp respects the °F/°C pref)
function SensorVals({ m, unit }) {
  return html`<div class="sensor-vals">
    ${m.temperature_c != null && html`<span class="sv"><b>${round1(convT(m.temperature_c, unit))}°</b>${unit}</span>`}
    ${m.humidity_pct != null && html`<span class="sv"><b>${round1(m.humidity_pct)}</b>%RH</span>`}
    ${m.co2_ppm != null && html`<span class="sv"><b>${Math.round(m.co2_ppm)}</b>ppm</span>`}
    ${m.radon_bqm3 != null && html`<span class="sv"><b>${Math.round(m.radon_bqm3)}</b>Bq</span>`}
    ${m.pressure_hpa != null && html`<span class="sv"><b>${Math.round(m.pressure_hpa)}</b>hPa</span>`}
  </div>`;
}

// minimized preview — one compact grid cell. Click to expand (handled by the parent).
function SensorChip({ s, onOpen }) {
  const m = s.metrics || {};
  const unit = useTemp();
  const stale = s.age_s != null && s.age_s > 1800;             // 30m: meters report often
  return html`
    <div class="sensor" onClick=${onOpen}>
      <div class="sensor-name">${dispName(s)} <span class="chev">▸</span></div>
      <div class="sensor-area">${dispRoom(s)}</div>
      <${SensorVals} m=${m} unit=${unit} />
      <div class="sensor-meta">
        <span class=${stale ? "age-stale" : "age-fresh"}>${fmtAge(s.age_s)}</span>
        ${m.battery_pct != null && html` · 🔋 ${Math.round(m.battery_pct)}%`}
      </div>
    </div>`;
}

// expanded — full width, below the grid, charts over the shared range. Click the header to collapse.
function ExpandedSensor({ s, range, isAdmin, onEdit, onClose }) {
  const m = s.metrics || {};
  const unit = useTemp();
  const [series, setSeries] = useState(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let alive = true; setSeries(null); setErr("");
    (async () => {
      try {
        const keys = GRAPHABLE.filter((g) => m[g.key] != null).map((g) => g.key);
        const out = {};
        for (const k of keys) out[k] = await fetchReadingsRange(s.device_id, k, range.start, range.end);
        if (alive) setSeries(out);
      } catch (e) { if (alive) setErr(String(e.message)); }
    })();
    return () => { alive = false; };
  }, [s.device_id, range.start, range.end]);
  return html`
    <div class="sensor open">
      <div class="sensor-head">
        <span class="sensor-name" onClick=${onClose}>${dispName(s)} <span class="chev">▾</span></span>
        <span class="sensor-area">${dispRoom(s)}</span>
        <button class="btn sm ghost edit-btn" onClick=${() => onEdit(s)}>✎</button>
      </div>
      <${SensorVals} m=${m} unit=${unit} />
      <div class="charts">
        ${err && html`<div class="err">${err}</div>`}
        ${series == null && !err && html`<div class="note">loading…</div>`}
        ${series && GRAPHABLE.filter((g) => series[g.key]).map((g) => html`
          <div class="chart-block" key=${g.key}>
            <div class="chart-label">${g.label}</div>
            <${AdaptiveChart} traces=${[{ label: g.label, color: g.color, unit: g.unit, metric: g.key, points: series[g.key] }]} />
          </div>`)}
      </div>
    </div>`;
}

function Sensors({ sensors, isAdmin, onEdit, onChange }) {
  const [expanded, setExpanded] = useState(new Set());        // device_ids currently expanded
  const [range, setRange] = useState(() => computeRange(24, { start: "", end: "" }));
  const [hiddenList, setHiddenList] = useState(null);         // null=not loaded, []=loaded
  if (sensors == null) return null;
  if (sensors.length === 0) return html`<p class="note">No sensor readings yet.</p>`;
  const open = (id) => setExpanded((p) => new Set(p).add(id));
  const close = (id) => setExpanded((p) => { const n = new Set(p); n.delete(id); return n; });
  const mins = sensors.filter((s) => !expanded.has(s.device_id));
  const exps = sensors.filter((s) => expanded.has(s.device_id));

  const loadHidden = async () => {
    try {
      const d = await getJSON("/api/v1/devices/meta");
      setHiddenList(Object.entries(d.meta || {}).filter(([, m]) => m.hidden)
        .map(([id, m]) => ({ device_id: id, ...m })));
    } catch { setHiddenList([]); }
  };
  const unhide = async (id) => {
    try { await adminSend("PUT", `/api/v1/devices/${id}/meta`, { hidden: false }); } catch {}
    await onChange(); loadHidden();
  };

  return html`
    <div class="sensors-wrap">
      <h2 class="section">Sensors</h2>
      <div class="sensor-grid">
        ${mins.map((s) => html`<${SensorChip} key=${s.device_id} s=${s} onOpen=${() => open(s.device_id)} />`)}
      </div>
      ${exps.length > 0 && html`
        <div class="expanded-list">
          <${RangeControl} onRange=${setRange} />
          ${exps.map((s) => html`<${ExpandedSensor} key=${s.device_id} s=${s} range=${range}
            isAdmin=${isAdmin} onEdit=${onEdit} onClose=${() => close(s.device_id)} />`)}
        </div>`}
      ${isAdmin && html`<div class="hidden-ctl">
        ${hiddenList == null
          ? html`<button class="btn sm ghost" onClick=${loadHidden}>show hidden devices</button>`
          : hiddenList.length === 0
            ? html`<span class="note">no hidden devices</span>`
            : html`<span class="note">hidden — tap to restore:</span> ${hiddenList.map((h) => html`
                <span class="trace-chip" onClick=${() => unhide(h.device_id)}>${h.name || prettyName(h.device_id)} ↺</span>`)}`}
      </div>`}
    </div>`;
}

// ── graph builder (multiple panels, each with arbitrary traces; sensors + weather) ───────────
const RANGES = [{ label: "6h", h: 6 }, { label: "24h", h: 24 }, { label: "7d", h: 168 }, { label: "30d", h: 720 }];
const computeRange = (hours, custom) => (custom.start && custom.end)
  ? { start: isoNoMs(new Date(custom.start)), end: isoNoMs(new Date(custom.end)) }
  : { start: isoNoMs(new Date(Date.now() - hours * 3600 * 1000)), end: isoNoMs(new Date()) };

// shared time-range picker (presets + custom datetimes). Owns its state; reports the chosen window via
// onRange(range). Used by both the graph builder and the expanded sensor readouts.
function RangeControl({ onRange }) {
  const [hours, setHours] = useState(24);
  const [custom, setCustom] = useState({ start: "", end: "" });
  const preset = (h) => { setHours(h); setCustom({ start: "", end: "" }); onRange(computeRange(h, { start: "", end: "" })); };
  const cust = (c) => { setCustom(c); if (c.start && c.end) onRange(computeRange(null, c)); };
  const refresh = () => onRange(computeRange(hours, custom));
  return html`
    <div class="range-sel">
      ${RANGES.map((r) => html`<button class="btn sm ${!custom.start && hours === r.h ? "primary" : ""}"
        onClick=${() => preset(r.h)}>${r.label}</button>`)}
      <span class="range-custom">
        <input type="datetime-local" value=${custom.start} onInput=${(e) => cust({ ...custom, start: e.target.value })} />
        <input type="datetime-local" value=${custom.end} onInput=${(e) => cust({ ...custom, end: e.target.value })} />
      </span>
      <button class="btn sm ghost" onClick=${refresh} title="refresh">↻</button>
    </div>`;
}

// overlay traces on one chart. Same unit → shared scale (real values); mixed units → per-trace
// normalized (trend comparison), each trace's real range shown in the legend. Temp converted to pref.
function AdaptiveChart({ traces }) {
  const unit = useTemp();
  const series = traces.map((t) => prepSeries(t, unit)).filter((t) => t.points.length >= 2);
  if (!series.length) return html`<div class="note">no data in range</div>`;
  const W = 320, H = 150, pad = 8;
  const allT = series.flatMap((t) => t.points.map((p) => p.t));
  const tMin = Math.min(...allT), tMax = Math.max(...allT);
  const spanH = (tMax - tMin) / 3600000;
  const x = (t) => pad + ((t - tMin) / (tMax - tMin || 1)) * (W - 2 * pad);
  const units = [...new Set(series.map((t) => t.unit))];
  const shared = units.length === 1;

  let yOf, yLabels;
  if (shared) {
    const allV = series.flatMap((t) => t.points.map((p) => p.v));
    let mn = Math.min(...allV), mx = Math.max(...allV); if (mn === mx) { mn -= 1; mx += 1; }
    yOf = series.map(() => (v) => pad + (1 - (v - mn) / (mx - mn)) * (H - 2 * pad));
    const u = units[0];
    yLabels = [`${round1(mx)}${u}`, `${round1((mn + mx) / 2)}${u}`, `${round1(mn)}${u}`];
  } else {
    yOf = series.map((t) => {
      let mn = Math.min(...t.points.map((p) => p.v)), mx = Math.max(...t.points.map((p) => p.v));
      if (mn === mx) { mn -= 1; mx += 1; }
      t._mn = mn; t._mx = mx;
      return (v) => pad + (1 - (v - mn) / (mx - mn)) * (H - 2 * pad);
    });
    yLabels = ["100%", "50%", "0%"];                   // mixed units → each trace normalized to its range
  }
  const gridY = [pad, H / 2, H - pad];
  return html`
    <div class="chart2">
      <div class="chart2-row">
        <div class="yax">${yLabels.map((l) => html`<span>${l}</span>`)}</div>
        <svg viewBox="0 0 ${W} ${H}" class="chart" preserveAspectRatio="none">
          ${gridY.map((yy) => html`<line x1="0" y1=${yy} x2=${W} y2=${yy} class="grid" />`)}
          ${series.map((t, i) => html`<path fill="none" stroke=${t.color} stroke-width="1.5"
            d=${t.points.map((p, j) => `${j ? "L" : "M"}${x(p.t).toFixed(1)} ${yOf[i](p.v).toFixed(1)}`).join(" ")} />`)}
        </svg>
      </div>
      <div class="xax">
        <span>${fmtClock(tMin, spanH)}</span>
        <span>${fmtClock((tMin + tMax) / 2, spanH)}</span>
        <span>${fmtClock(tMax, spanH)}</span>
      </div>
      ${!shared && html`<div class="note ax-note">normalized · mixed units (each trace's real range below)</div>`}
      <div class="legend">
        ${series.map((t) => html`<span class="leg">
          <svg class="sw" viewBox="0 0 12 12" width="12" height="12"><rect width="12" height="12" rx="2" fill=${t.color} /></svg>
          <span style=${{ color: t.color }}>${t.label}</span>${
          shared ? "" : ` (${round1(t._mn)}–${round1(t._mx)}${t.unit})`}</span>`)}
      </div>
    </div>`;
}

// one graph panel: a user-chosen set of traces + their plot. Fetches when its traces or the range change.
function Panel({ catalog, panel, range, onToggleTrace, onRemove }) {
  const [series, setSeries] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const catRef = useRef(catalog); catRef.current = catalog;
  const keysStr = panel.keys.join(",");
  useEffect(() => {
    let alive = true;
    if (!panel.keys.length) { setSeries([]); return; }
    setLoading(true); setErr("");
    (async () => {
      try {
        const out = []; let ci = 0;
        for (const k of panel.keys) {
          const tr = catRef.current.find((c) => c.key === k); if (!tr) continue;
          const pts = await fetchTrace(tr, range.start, range.end);
          out.push({ ...tr, color: PALETTE[ci++ % PALETTE.length], points: pts });
        }
        if (alive) setSeries(out);
      } catch (e) { if (alive) setErr(String(e.message)); }
      if (alive) setLoading(false);
    })();
    return () => { alive = false; };
  }, [keysStr, range.start, range.end]);

  const chosen = new Set(panel.keys);
  return html`
    <div class="panel">
      <div class="panel-traces">
        ${panel.keys.map((k) => {
          const tr = catalog.find((c) => c.key === k);
          return html`<span class="trace-chip" onClick=${() => onToggleTrace(k)}>${tr ? tr.label : k} ✕</span>`;
        })}
        <select class="trace-add" value=""
          onChange=${(e) => { if (e.target.value) { onToggleTrace(e.target.value); e.target.value = ""; } }}>
          <option value="">+ add trace…</option>
          ${catalog.filter((c) => !chosen.has(c.key)).map((c) => html`<option value=${c.key}>${c.label}</option>`)}
        </select>
        <button class="btn sm ghost panel-x" onClick=${onRemove}>remove</button>
      </div>
      ${loading && html`<div class="note">loading…</div>`}
      ${err && html`<div class="err">${err}</div>`}
      ${series && !loading && (panel.keys.length === 0
        ? html`<div class="note">add a trace to plot.</div>`
        : html`<${AdaptiveChart} traces=${series} />`)}
    </div>`;
}

function GraphBuilder({ sensors, weather }) {
  const catalog = traceCatalog(sensors, weather);
  const [panels, setPanels] = useState([{ id: 1, keys: [] }]);
  const [range, setRange] = useState(() => computeRange(24, { start: "", end: "" }));
  const nextId = useRef(2);
  const addPanel = () => setPanels((p) => [...p, { id: nextId.current++, keys: [] }]);
  const removePanel = (id) => setPanels((p) => p.filter((x) => x.id !== id));
  const toggleTrace = (id, key) => setPanels((p) => p.map((x) => x.id !== id ? x
    : { ...x, keys: x.keys.includes(key) ? x.keys.filter((k) => k !== key) : [...x.keys, key] }));

  if (!catalog.length) return null;
  return html`
    <div class="explore">
      <h2 class="section">Graphs</h2>
      <${RangeControl} onRange=${setRange} />
      ${panels.map((pn) => html`
        <${Panel} key=${pn.id} catalog=${catalog} panel=${pn} range=${range}
          onToggleTrace=${(k) => toggleTrace(pn.id, k)} onRemove=${() => removePanel(pn.id)} />`)}
      <button class="btn sm" onClick=${addPanel}>+ Add graph</button>
    </div>`;
}

// ── admin unlock modal ───────────────────────────────────────────────────────
// ── device edit (R8: friendly name / room / hide) ────────────────────────────
function DeviceMetaModal({ device, onClose, onSaved }) {
  const [name, setName] = useState(device.name || "");
  const [room, setRoom] = useState(device.room || (device.area ? prettyArea(device.area) : ""));
  const [hidden, setHidden] = useState(!!device.hidden);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const save = async () => {
    setBusy(true); setErr("");
    try {
      await adminSend("PUT", `/api/v1/devices/${encodeURIComponent(device.device_id)}/meta`,
                      { name, room, hidden });
      onSaved(); onClose();
    } catch (e) { setErr(String(e.message)); setBusy(false); }
  };
  return html`
    <div class="modal-bg" onClick=${onClose}>
      <div class="modal" onClick=${(e) => e.stopPropagation()}>
        <h3>Edit device</h3>
        <p class="note">${device.device_id}</p>
        <input value=${name} placeholder=${`name (default: ${prettyName(device.device_id)})`}
          onInput=${(e) => setName(e.target.value)} />
        <input value=${room} placeholder="room" onInput=${(e) => setRoom(e.target.value)} />
        <label class="switch"><input type="checkbox" checked=${hidden}
          onChange=${(e) => setHidden(e.target.checked)} /> Hide from dashboard</label>
        ${err && html`<div class="err">${err}</div>`}
        <div class="modal-actions">
          <button class="btn ghost" onClick=${onClose}>Cancel</button>
          <button class="btn primary" disabled=${busy} onClick=${save}>${busy ? "Saving…" : "Save"}</button>
        </div>
      </div>
    </div>`;
}

function AdminModal({ onClose, onUnlock }) {
  const [pw, setPw] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const inputRef = useRef(null);
  useEffect(() => { inputRef.current && inputRef.current.focus(); }, []);
  const submit = async () => {
    if (!pw) return;
    setBusy(true); setErr("");
    const tok = await deriveToken(pw);
    // VERIFY against the server before accepting — otherwise a wrong password "succeeds" locally and
    // only fails later on the first command. auth/check 200s only for a valid bearer.
    let ok = false;
    try {
      const r = await fetch("/control/auth/check", { headers: { Authorization: "Bearer " + tok } });
      ok = r.ok;
    } catch { ok = false; }
    if (ok) { setToken(tok); onUnlock(); onClose(); }
    else { setErr("Incorrect password"); setBusy(false); }
  };
  return html`
    <div class="modal-bg" onClick=${onClose}>
      <div class="modal" onClick=${(e) => e.stopPropagation()}>
        <h3>Admin unlock</h3>
        <p class="note">Enter the master passphrase. Only the derived token is stored locally.</p>
        <input ref=${inputRef} type="password" value=${pw} placeholder="master passphrase"
          onInput=${(e) => { setPw(e.target.value); setErr(""); }}
          onKeyDown=${(e) => e.key === "Enter" && submit()} />
        ${err && html`<div class="err">${err}</div>`}
        <div class="modal-actions">
          <button class="btn ghost" onClick=${onClose}>Cancel</button>
          <button class="btn primary" disabled=${busy || !pw} onClick=${submit}>${busy ? "Checking…" : "Unlock"}</button>
        </div>
      </div>
    </div>`;
}

// ── alerts banner ────────────────────────────────────────────────────────────
function AlertsBanner({ alerts }) {
  if (!alerts || !alerts.length) return null;
  const icon = { critical: "⛔", warning: "⚠️", info: "ℹ️" };
  return html`
    <div class="alerts">
      ${alerts.map((a) => html`
        <div class="alert ${a.severity}" key=${a.kind + a.device_id}>
          <span class="alert-ic">${icon[a.severity] || "•"}</span>
          <span class="alert-name">${a.name}</span>
          <span class="alert-detail">${a.detail}</span>
        </div>`)}
    </div>`;
}

// ── app shell ────────────────────────────────────────────────────────────────
function App() {
  const [devices, setDevices] = useState(null);
  const [sensors, setSensors] = useState(null);
  const [alerts, setAlerts] = useState([]);
  const [weather, setWeather] = useState(null);
  const [status, setStatus] = useState("init");      // init | live | down
  const [isAdmin, setIsAdmin] = useState(!!getToken());
  const [showAdmin, setShowAdmin] = useState(false);
  const [tempUnit, setTempUnit] = useState(tempPref());
  const [editDevice, setEditDevice] = useState(null);   // R8 edit modal target
  const onEdit = (d) => (isAdmin ? setEditDevice(d) : setShowAdmin(true));

  // weather lane catalog (locations + metrics) — fetched once for the graph builder
  useEffect(() => { getJSON("/weather/meta").then(setWeather).catch(() => {}); }, []);
  const toggleUnit = () => setTempUnit((u) => {
    const n = u === "F" ? "C" : "F"; localStorage.setItem("ha.tempUnit", n); return n;
  });

  const refresh = useCallback(async () => {
    try {
      const [disp, sens, alr] = await Promise.all([
        getJSON("/api/v1/displays"),
        getJSON("/api/v1/sensors"),
        getJSON("/api/v1/alerts"),
      ]);
      setDevices(disp.devices || []);
      setSensors(sens.sensors || []);
      setAlerts(alr.alerts || []);
      setStatus("live");
    } catch {
      setStatus("down");
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [refresh]);

  const lock = () => { setToken(""); setIsAdmin(false); };

  return html`
    <${UnitsCtx.Provider} value=${tempUnit}>
    <div class="wrap">
      <div class="topbar">
        <div class="dot ${status === "live" ? "live" : status === "down" ? "down" : ""}"></div>
        <h1>Home Automation</h1>
        <span class="build">${BUILD}</span>
        <div class="spacer"></div>
        <button class="btn sm ghost" onClick=${toggleUnit} title="temperature unit">°${tempUnit}</button>
        ${isAdmin
          ? html`<span class="admin-on" title="Admin unlocked">🔓 Admin</span>
                 <button class="btn sm ghost" onClick=${lock}>Lock</button>`
          : html`<button class="btn sm" onClick=${() => setShowAdmin(true)}>🔒 Admin</button>`}
      </div>

      <${AlertsBanner} alerts=${alerts} />

      ${devices == null && html`<div class="empty">Loading…</div>`}
      ${devices && devices.length > 0 && html`<h2 class="section">Automations</h2>`}
      ${devices && devices.map((vm) => html`
        <${DeviceCard} key=${vm.device_id} vm=${vm} sensors=${sensors} isAdmin=${isAdmin}
          onChange=${refresh} onNeedAdmin=${() => setShowAdmin(true)} onEdit=${onEdit} />`)}

      <${Sensors} sensors=${sensors} isAdmin=${isAdmin} onEdit=${onEdit} onChange=${refresh} />
      <${GraphBuilder} sensors=${sensors} weather=${weather} />

      ${status === "down" && html`<p class="note">⚠ Can't reach the server — showing last known state.</p>`}

      ${showAdmin && html`<${AdminModal} onClose=${() => setShowAdmin(false)}
        onUnlock=${() => setIsAdmin(true)} />`}
      ${editDevice && html`<${DeviceMetaModal} device=${editDevice}
        onClose=${() => setEditDevice(null)} onSaved=${refresh} />`}
    </div>
    </${UnitsCtx.Provider}>`;
}

render(html`<${App} />`, document.getElementById("root"));

// register the service worker (offline app-shell). Best-effort; ignore on http/file contexts.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/app/sw.js").catch(() => {});
}
