// Home Automation — web app (Preact + HTM, no build step). The vendored runtime IS the dependency;
// this file is plain readable JS you can edit on the box and reload. Talks to the API/BFF we serve.
import {
  html, render, useState, useEffect, useRef, useCallback,
} from "/app/vendor/preact-htm.standalone.module.js";

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
const range = (a, b) => Array.from({ length: b - a + 1 }, (_, i) => a + i);
const steps = (a, b, step) => { const o = []; for (let v = a; v <= b; v += step) o.push(v); return o; };
const isoNoMs = (d) => d.toISOString().replace(/\.\d+Z$/, "Z");

// pull a metric's time-series for a device over the last `hoursBack` hours (bounded server-side).
async function fetchReadings(deviceId, metric, hoursBack = 24) {
  const end = new Date();
  const start = new Date(end.getTime() - hoursBack * 3600 * 1000);
  const q = `start=${isoNoMs(start)}&end=${isoNoMs(end)}&metric=${metric}&limit=400`;
  const d = await getJSON(`/devices/${encodeURIComponent(deviceId)}/readings?${q}`);
  return (d.readings || []).map((r) => ({ t: Date.parse(r.ts), v: r.value })).filter((p) => !isNaN(p.t));
}

// ── tiny SVG line chart (no dependency) ──────────────────────────────────────
function LineChart({ series, color = "#4aa3ff", unit = "" }) {
  if (!series || series.length < 2) return html`<div class="note">not enough data yet</div>`;
  const W = 320, H = 110, pad = 6;
  const ts = series.map((p) => p.t), vs = series.map((p) => p.v);
  const tMin = Math.min(...ts), tMax = Math.max(...ts);
  let vMin = Math.min(...vs), vMax = Math.max(...vs);
  if (vMin === vMax) { vMin -= 1; vMax += 1; }
  const x = (t) => pad + ((t - tMin) / (tMax - tMin || 1)) * (W - 2 * pad);
  const y = (v) => pad + (1 - (v - vMin) / (vMax - vMin)) * (H - 2 * pad);
  const d = series.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)} ${y(p.v).toFixed(1)}`).join(" ");
  const last = series[series.length - 1].v;
  return html`
    <svg viewBox="0 0 ${W} ${H}" class="chart" preserveAspectRatio="none">
      <path d=${d} fill="none" stroke=${color} stroke-width="2" stroke-linejoin="round" />
    </svg>
    <div class="chart-ax">
      <span>min ${round1(vMin)}${unit}</span>
      <span>now <b>${round1(last)}${unit}</b></span>
      <span>max ${round1(vMax)}${unit}</span>
    </div>`;
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
      <div class="field"><label>Turn ON ≥ (%RH)</label>
        <input type="number" value=${onAbove} onInput=${(e) => setOnAbove(e.target.value)} /></div>
      <div class="field"><label>Turn OFF < (%RH)</label>
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
function DeviceCard({ vm, sensors, isAdmin, onChange, onNeedAdmin }) {
  const running = vm.running;
  const s = vm.sensor, o = vm.onboard, d = vm.last_decision;
  const ageStale = vm.health === "stale";   // the BFF already derives staleness; mirror it on the age
  return html`
    <div class="card health-${vm.health}">
      <div class="card-head">
        <h2>${vm.device_id}</h2>
        <span class="badge health-${vm.health}">${vm.health}</span>
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

// which metrics get a graph, with display unit + line color
const GRAPHABLE = [
  { key: "temperature_c", unit: "°C", color: "#f87171", label: "Temperature" },
  { key: "humidity_pct", unit: "%RH", color: "#4aa3ff", label: "Humidity" },
  { key: "co2_ppm", unit: "ppm", color: "#fbbf24", label: "CO₂" },
  { key: "radon_bqm3", unit: "Bq", color: "#a78bfa", label: "Radon" },
  { key: "pressure_hpa", unit: "hPa", color: "#34d399", label: "Pressure" },
];

function SensorChip({ s }) {
  const m = s.metrics || {};
  const stale = s.age_s != null && s.age_s > 1800;             // 30m: meters report often
  const [open, setOpen] = useState(false);
  const [series, setSeries] = useState(null);                  // {metricKey: [{t,v}]}
  const [err, setErr] = useState("");

  useEffect(() => {
    if (!open) return;
    let alive = true;
    setSeries(null); setErr("");
    (async () => {
      try {
        const keys = GRAPHABLE.filter((g) => m[g.key] != null).map((g) => g.key);
        const out = {};
        for (const k of keys) out[k] = await fetchReadings(s.device_id, k, 24);
        if (alive) setSeries(out);
      } catch (e) { if (alive) setErr(String(e.message)); }
    })();
    return () => { alive = false; };
  }, [open]);

  return html`
    <div class="sensor ${open ? "open" : ""}" onClick=${() => setOpen(!open)}>
      <div class="sensor-name">${prettyName(s.device_id)} <span class="chev">${open ? "▾" : "▸"}</span></div>
      <div class="sensor-vals">
        ${m.temperature_c != null && html`<span class="sv"><b>${round1(m.temperature_c)}°</b>C</span>`}
        ${m.humidity_pct != null && html`<span class="sv"><b>${round1(m.humidity_pct)}</b>%RH</span>`}
        ${m.co2_ppm != null && html`<span class="sv"><b>${Math.round(m.co2_ppm)}</b>ppm</span>`}
        ${m.radon_bqm3 != null && html`<span class="sv"><b>${Math.round(m.radon_bqm3)}</b>Bq</span>`}
        ${m.pressure_hpa != null && html`<span class="sv"><b>${Math.round(m.pressure_hpa)}</b>hPa</span>`}
      </div>
      <div class="sensor-meta">
        <span class=${stale ? "age-stale" : "age-fresh"}>${fmtAge(s.age_s)}</span>
        ${m.battery_pct != null && html` · 🔋 ${Math.round(m.battery_pct)}%`}
      </div>
      ${open && html`
        <div class="charts" onClick=${(e) => e.stopPropagation()}>
          <div class="charts-head">last 24h</div>
          ${err && html`<div class="err">${err}</div>`}
          ${series == null && !err && html`<div class="note">loading…</div>`}
          ${series && GRAPHABLE.filter((g) => series[g.key]).map((g) => html`
            <div class="chart-block" key=${g.key}>
              <div class="chart-label">${g.label}</div>
              <${LineChart} series=${series[g.key]} color=${g.color} unit=${g.unit} />
            </div>`)}
        </div>`}
    </div>`;
}

function Sensors({ sensors }) {
  if (sensors == null) return null;
  if (sensors.length === 0) return html`<p class="note">No sensor readings yet.</p>`;
  // group by area, preserving the server's area-sorted order
  const areas = [];
  const byArea = {};
  for (const s of sensors) {
    if (!byArea[s.area]) { byArea[s.area] = []; areas.push(s.area); }
    byArea[s.area].push(s);
  }
  return html`
    <div class="sensors-wrap">
      <h2 class="section">Sensors</h2>
      ${areas.map((a) => html`
        <div class="area" key=${a}>
          <div class="area-label">${prettyArea(a)}</div>
          <div class="sensor-grid">
            ${byArea[a].map((s) => html`<${SensorChip} key=${s.device_id} s=${s} />`)}
          </div>
        </div>`)}
    </div>`;
}

// ── admin unlock modal ───────────────────────────────────────────────────────
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

// ── app shell ────────────────────────────────────────────────────────────────
function App() {
  const [devices, setDevices] = useState(null);
  const [sensors, setSensors] = useState(null);
  const [status, setStatus] = useState("init");      // init | live | down
  const [isAdmin, setIsAdmin] = useState(!!getToken());
  const [showAdmin, setShowAdmin] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [disp, sens] = await Promise.all([
        getJSON("/api/v1/displays"),
        getJSON("/api/v1/sensors"),
      ]);
      setDevices(disp.devices || []);
      setSensors(sens.sensors || []);
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
    <div class="wrap">
      <div class="topbar">
        <div class="dot ${status === "live" ? "live" : status === "down" ? "down" : ""}"></div>
        <h1>Home Automation</h1>
        <div class="spacer"></div>
        ${isAdmin
          ? html`<span class="admin-on" title="Admin unlocked">🔓 Admin</span>
                 <button class="btn sm ghost" onClick=${lock}>Lock</button>`
          : html`<button class="btn sm" onClick=${() => setShowAdmin(true)}>🔒 Admin</button>`}
      </div>

      ${devices == null && html`<div class="empty">Loading…</div>`}
      ${devices && devices.length > 0 && html`<h2 class="section">Automations</h2>`}
      ${devices && devices.map((vm) => html`
        <${DeviceCard} key=${vm.device_id} vm=${vm} sensors=${sensors} isAdmin=${isAdmin}
          onChange=${refresh} onNeedAdmin=${() => setShowAdmin(true)} />`)}

      <${Sensors} sensors=${sensors} />

      ${status === "down" && html`<p class="note">⚠ Can't reach the server — showing last known state.</p>`}

      ${showAdmin && html`<${AdminModal} onClose=${() => setShowAdmin(false)}
        onUnlock=${() => setIsAdmin(true)} />`}
    </div>`;
}

render(html`<${App} />`, document.getElementById("root"));

// register the service worker (offline app-shell). Best-effort; ignore on http/file contexts.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/app/sw.js").catch(() => {});
}
