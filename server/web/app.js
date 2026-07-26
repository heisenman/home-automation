// Home Automation — web app (Preact + HTM, no build step). The vendored runtime IS the dependency;
// this file is plain readable JS you can edit on the box and reload. Talks to the API/BFF we serve.
import {
  html, render, useState, useEffect, useRef, useCallback, useMemo, createContext, useContext,
} from "/app/vendor/preact-htm.standalone.module.js";
import { pushSupported, pushState, enablePush, disablePush } from "/app/push.js";

// ── units (°C/°F) ────────────────────────────────────────────────────────────
// Temperatures are STORED in Celsius (SI). The UI converts for display only. Preference persisted.
const UnitsCtx = createContext("F");
const useTemp = () => useContext(UnitsCtx);
const tempPref = () => localStorage.getItem("ha.tempUnit") || "F";
const isTempMetric = (m) => m === "temperature_c" || m === "dewpoint_c";   // both convert °C↔°F
const convT = (c, unit) => (unit === "F" ? c * 9 / 5 + 32 : c);
const tUnit = (unit) => (unit === "F" ? "°F" : "°C");

// ── admin token ──────────────────────────────────────────────────────────────
// The control endpoints want Authorization: Bearer SHA256("ha-api:"+master). We derive that hash in the
// browser (Web Crypto) and store ONLY the derived token — never the master — so a peek at localStorage
// can't recover the passphrase. Read-only views need no token.
const TOKEN_KEY = "ha.adminToken";
const getToken = () => localStorage.getItem(TOKEN_KEY) || "";
const setToken = (t) => (t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY));

// Pure-JS SHA-256 (hex). We CANNOT use crypto.subtle: it only exists in a "secure context" (HTTPS or
// localhost), and this app is served over plain HTTP on the LAN — so crypto.subtle is undefined there and
// the old deriveToken threw, hanging the login on "Checking…". TextEncoder/DataView ARE always available.
const _K256 = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);
function sha256hex(str) {
  const rotr = (x, n) => ((x >>> n) | (x << (32 - n))) >>> 0;
  const msg = new TextEncoder().encode(str);
  const l = msg.length, withOne = l + 1;
  const total = withOne + ((56 - (withOne % 64) + 64) % 64) + 8;
  const buf = new Uint8Array(total);
  buf.set(msg); buf[l] = 0x80;
  const dv = new DataView(buf.buffer), bitLen = l * 8;
  dv.setUint32(total - 8, Math.floor(bitLen / 0x100000000));
  dv.setUint32(total - 4, bitLen >>> 0);
  let h0 = 0x6a09e667, h1 = 0xbb67ae85, h2 = 0x3c6ef372, h3 = 0xa54ff53a,
    h4 = 0x510e527f, h5 = 0x9b05688c, h6 = 0x1f83d9ab, h7 = 0x5be0cd19;
  const w = new Uint32Array(64);
  for (let off = 0; off < total; off += 64) {
    for (let i = 0; i < 16; i++) w[i] = dv.getUint32(off + i * 4);
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }
    let a = h0, b = h1, c = h2, d = h3, e = h4, f = h5, g = h6, h = h7;
    for (let i = 0; i < 64; i++) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const t1 = (h + S1 + ch + _K256[i] + w[i]) >>> 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const t2 = (S0 + maj) >>> 0;
      h = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b; b = a; a = (t1 + t2) >>> 0;
    }
    h0 = (h0 + a) >>> 0; h1 = (h1 + b) >>> 0; h2 = (h2 + c) >>> 0; h3 = (h3 + d) >>> 0;
    h4 = (h4 + e) >>> 0; h5 = (h5 + f) >>> 0; h6 = (h6 + g) >>> 0; h7 = (h7 + h) >>> 0;
  }
  const hx = (x) => x.toString(16).padStart(8, "0");
  return hx(h0) + hx(h1) + hx(h2) + hx(h3) + hx(h4) + hx(h5) + hx(h6) + hx(h7);
}
// self-test: if this fails, the impl has a bug — surface it instead of producing a wrong token.
const _SHA_OK = sha256hex("abc") === "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";

async function deriveToken(master) {
  const msg = "ha-api:" + master;
  // R9: in a secure context (HTTPS:8443) use the native WebCrypto digest — audited + fast. Plain HTTP
  // (:8123, kept for healthcheck/local tools) has no crypto.subtle, so fall back to the pure-JS SHA-256.
  // Both yield the identical SHA-256("ha-api:"+master) bearer, so the server verifies either the same way.
  if (window.isSecureContext && window.crypto && crypto.subtle) {
    try {
      const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(msg));
      return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
    } catch { /* fall through to the JS implementation */ }
  }
  if (!_SHA_OK) throw new Error("SHA-256 self-test failed (report this)");
  return sha256hex(msg);
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
const BUILD = "v49 Add-sensor discovery — see nearby unregistered BLE devices + one-click intake";

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
// Renders the server-authored `override` control (shared-ui-spec): presets + action contract come from
// vm.controls, not hardcoded here. Falls back to the legacy Off/Boost/Resume set if the spec is absent
// (migration safety). Behavior preserved: off/boost always shown, clear only while an override is active.
function OverrideControls({ vm, isAdmin, onChange, onNeedAdmin }) {
  const id = vm.device_id;
  const override = vm.override;
  const spec = (vm.controls || []).find((c) => c.kind === "override");
  const presets = spec?.presets || [
    { action: "off", duration_min: 60, label: "Off 1h" },
    { action: "boost_on", duration_min: 60, label: "Boost 1h" },
    { action: "clear", label: "Resume auto", when: "override_active" },
  ];
  const method = spec?.action?.method || "POST";
  const path = (spec?.action?.path || "/control/{id}/override").replace("{id}", id);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const act = async (p) => {
    if (!isAdmin) return onNeedAdmin();
    setBusy(true); setErr("");
    try {
      const body = p.action === "clear" ? { action: "clear" }
        : { action: p.action, duration_min: p.duration_min };
      await adminSend(method, path, body);
      await onChange();
    } catch (e) { setErr(String(e.message)); }
    setBusy(false);
  };
  const left = override && override.expires_in_min != null ? Math.ceil(override.expires_in_min) : null;
  return html`
    <div class="controls">
      ${presets.filter((p) => p.when !== "override_active" || override).map((p) => html`
        <button class="btn sm ${p.action === "clear" ? "ghost" : ""}" disabled=${busy}
          onClick=${() => act(p)}>${p.label}</button>`)}
      ${override && html`<span class="note">override: <b>${override.action}</b>${
          left != null ? ` · ${left}m left` : ""}</span>`}
      ${err && html`<span class="err">${err}</span>`}
    </div>`;
}

// ── decision history ("why is it on?") ───────────────────────────────────────
function DecisionHistory({ decisions }) {
  const [open, setOpen] = useState(false);
  if (!decisions || !decisions.length) return null;
  return html`
    <div class="settings">
      <button class="btn sm ghost" onClick=${() => setOpen(!open)}>
        ${open ? "▾" : "▸"} Why? · last ${decisions.length} decisions</button>
      ${open && html`<div class="history">
        ${decisions.map((r, i) => html`<div class="hist-row" key=${i}>
          <span class="hist-ts">${(r.ts || "").slice(11, 16)}</span>
          <span class="hist-src src-${r.source}">${r.source}</span>
          <span class="hist-reason">${r.reason}${r.acted ? " · acted" : ""}</span>
        </div>`)}
      </div>`}
    </div>`;
}

// ── settings (app-mutable policy) ────────────────────────────────────────────
function SettingsPanel({ vm, sensors, isAdmin, onChange, onNeedAdmin }) {
  const c = vm.control || {};
  const [open, setOpen] = useState(false);
  const [enabled, setEnabled] = useState(!!c.enabled);
  const [strategy, setStrategy] = useState(c.strategy || "hysteresis");
  const [source, setSource] = useState(vm.control.source_sensor || "");
  const [fallbacks, setFallbacks] = useState(vm.control.fallback_sensors || []);
  const [onAbove, setOnAbove] = useState(c.on_above ?? "");
  const [offBelow, setOffBelow] = useState(c.off_below ?? "");
  const [quiet, setQuiet] = useState("");
  const [scenes, setScenes] = useState(vm.scenes || {});   // {Away:{...}, Sleep:{...}}
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [flash, setFlash] = useState("");

  // per-device scene profiles. Home is the neutral base (no profile); Away/Sleep can park the device or
  // relax its thresholds. behavior ∈ base | off | custom, derived from the stored profile.
  const EDITABLE_SCENES = ["Away", "Sleep"];
  const behaviorOf = (p) => (!p || !Object.keys(p).length ? "base" : p.off ? "off" : "custom");
  const setBehavior = (name, b) => setScenes((s) => {
    const next = { ...s };
    if (b === "base") delete next[name];
    else if (b === "off") next[name] = { off: true };
    else next[name] = { on_above: Number(onAbove) || 55, off_below: Number(offBelow) || 50 };
    return next;
  });
  const setSceneField = (name, k, v) => setScenes((s) => ({ ...s, [name]: { ...s[name], [k]: v } }));
  // Client-side scene validation — mirror the server rule (ON strictly > OFF, both filled) so an invalid
  // custom threshold is caught HERE with a clear message, instead of a small inline 400 after Save that
  // reads like a no-op. Returns "" when the scene is fine (base / park-off / valid custom).
  const sceneIssue = (name) => {
    const p = scenes[name];
    if (!p || p.off || !Object.keys(p).length) return "";
    const on = Number(p.on_above), off = Number(p.off_below);
    if (p.on_above === "" || p.off_below === "" || p.on_above == null || p.off_below == null
        || !Number.isFinite(on) || !Number.isFinite(off)) return "enter both ON and OFF";
    if (on <= off) return `ON (${on}) must be above OFF (${off})`;
    return "";
  };
  const firstSceneIssue = () => {
    for (const name of EDITABLE_SCENES) { const m = sceneIssue(name); if (m) return `${name}: ${m}`; }
    return "";
  };

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
    const sIssue = firstSceneIssue();
    if (sIssue) { setErr(sIssue); setFlash(""); return; }   // never POST an invalid scene threshold
    setBusy(true); setErr(""); setFlash("");
    const patch = {
      enabled,
      control: { strategy, on_above: Number(onAbove), off_below: Number(offBelow) },
    };
    if (source) patch.source_sensor = source;
    patch.fallback_sensors = fallbacks;
    if (quiet.trim()) patch.schedule = [{ when: quiet.trim(), policy: "off" }];
    // normalize scene profiles: drop empties, coerce thresholds to numbers
    const sc = {};
    for (const [name, p] of Object.entries(scenes)) {
      if (!p || !Object.keys(p).length) continue;
      if (p.off) sc[name] = { off: true };
      else sc[name] = { on_above: Number(p.on_above), off_below: Number(p.off_below) };
    }
    patch.scenes = sc;
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
      <div class="field"><label>Strategy</label>
        <select value=${strategy} onChange=${(e) => setStrategy(e.target.value)}>
          <option value="hysteresis">hysteresis (external sensor + thresholds)</option>
          <option value="setpoint">setpoint (trust the device's own loop)</option>
        </select></div>
      ${strategy === "hysteresis" ? html`
        <div class="field"><label>Humidity source</label>
          <select value=${source} onChange=${(e) => setSource(e.target.value)}>
            ${opts.length === 0 && html`<option value="">(no humidity sensors)</option>`}
            ${opts.map((o) => html`<option value=${o.id}>${o.label}</option>`)}
          </select></div>
        <div class="field"><label>Fallback sources</label>
          <div class="controls">
            ${fallbacks.map((id) => html`<span class="trace-chip"
              onClick=${() => setFallbacks(fallbacks.filter((x) => x !== id))}>${prettyName(id)} ✕</span>`)}
            <select class="trace-add" value=""
              onChange=${(e) => { if (e.target.value) { setFallbacks([...fallbacks, e.target.value]); e.target.value = ""; } }}>
              <option value="">+ add fallback…</option>
              ${opts.filter((o) => o.id !== source && !fallbacks.includes(o.id))
                .map((o) => html`<option value=${o.id}>${o.label}</option>`)}
            </select>
          </div></div>
        <div class="field"><label>Turn ON at/above (%RH)</label>
          <input type="number" value=${onAbove} onInput=${(e) => setOnAbove(e.target.value)} /></div>
        <div class="field"><label>Turn OFF below (%RH)</label>
          <input type="number" value=${offBelow} onInput=${(e) => setOffBelow(e.target.value)} /></div>`
        : html`<p class="note">setpoint: the device runs its own loop to its target — the dashboard just
          keeps it powered. (Not recommended here: this unit's onboard RH reads low.)</p>`}
      <div class="field"><label>Quiet window</label>
        <input type="text" placeholder="22:00-07:00 (optional)" value=${quiet}
          onInput=${(e) => setQuiet(e.target.value)} /></div>
      <div class="divider"></div>
      <div class="field"><label>Scene behavior</label>
        <p class="note">What each whole-house scene does to this device (Home = the settings above). Takes
          effect only while the whole-house scene is set to that name.</p></div>
      ${EDITABLE_SCENES.map((name) => {
        const p = scenes[name] || {};
        const b = behaviorOf(p);
        return html`<div class="field" key=${name}>
          <label>${SCENE_ICON[name] || ""} ${name}</label>
          <select value=${b} onChange=${(e) => setBehavior(name, e.target.value)}>
            <option value="base">same as Home</option>
            <option value="off">turn off (park it)</option>
            <option value="custom">custom thresholds</option>
          </select>
          ${b === "custom" && html`<div class="controls scene-thresh">
            <label class="inline">ON ≥<input type="number" value=${p.on_above ?? ""}
              onInput=${(e) => setSceneField(name, "on_above", e.target.value)} />%</label>
            <label class="inline">OFF <<input type="number" value=${p.off_below ?? ""}
              onInput=${(e) => setSceneField(name, "off_below", e.target.value)} />%</label>
          </div>`}
          ${b === "custom" && sceneIssue(name) && html`<span class="err sm">⚠ ${sceneIssue(name)}</span>`}
        </div>`;
      })}
      <div class="controls">
        <button class="btn sm primary" disabled=${busy || !!firstSceneIssue()} onClick=${save}>Save</button>
        <button class="btn sm ghost" disabled=${busy} onClick=${() => setOpen(false)}>Close</button>
        ${flash && html`<span class="ok-flash">${flash}</span>`}
        ${err && html`<span class="err">${err}</span>`}
      </div>
    </div>`;
}

// ── manual control (direct command set) ─────────────────────────────────────
// Renders the server-authored command controls (setpoint/ranged/indicator) from vm.controls — label,
// ranges, options, and the action contract all come from the spec, not from client-side trait mapping.
// Widget construction stays here (the "thin renderer"); the *decisions* live in the BFF (shared-ui-spec).
function ManualControl({ vm, isAdmin, onChange, onNeedAdmin }) {
  const act = vm.actuator || {};
  const cmds = (vm.controls || []).filter((c) =>
    c.kind === "setpoint" || c.kind === "ranged" || c.kind === "mode" || c.kind === "indicator");
  const spCtl = cmds.find((c) => c.kind === "setpoint");
  const rgCtl = cmds.find((c) => c.kind === "ranged");
  const modeCtl = cmds.find((c) => c.kind === "mode");
  const indCtl = cmds.find((c) => c.kind === "indicator");
  const spInit = spCtl ? (act[spCtl.now_key] ?? spCtl.safe_value ?? "") : "";
  const [open, setOpen] = useState(false);
  const [target, setTarget] = useState(spInit);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");

  if (!cmds.length) return null;                        // device exposes no manual functions

  // one issuer for every command kind: path/trait/action/arg_key all come from the control's action contract.
  const cmd = async (c, value, tag) => {
    if (!isAdmin) return onNeedAdmin();
    setBusy(tag); setErr("");
    try {
      const path = c.action.path.replace("{id}", vm.device_id);
      await adminSend(c.action.method, path,
        { trait: c.action.trait, action: c.action.action, args: { [c.action.arg_key]: value } });
      await onChange();
    } catch (e) { setErr(String(e.message)); }
    setBusy("");
  };

  if (!open) {
    return html`<div class="settings"><button class="btn sm ghost"
        onClick=${() => setOpen(true)}>🎛 Manual control</button></div>`;
  }
  const u = spCtl && spCtl.unit ? spCtl.unit : "";
  const spNow = spCtl ? act[spCtl.now_key] : null;
  const rgNow = rgCtl ? act[rgCtl.now_key] : null;
  const modeNow = modeCtl ? act[modeCtl.now_key] : null;
  const modeLabel = modeCtl && modeNow != null
    ? ((modeCtl.options.find((o) => o.value === modeNow) || {}).label ?? modeNow) : null;
  const indNow = indCtl ? act[indCtl.now_key] : null;
  return html`
    <div class="settings">
      <div class="divider"></div>
      <p class="note">Direct device commands. Power is automation-managed — use the override buttons
        above to force on/off.</p>
      ${spCtl && html`
        <div class="field"><label>${spCtl.label}</label>
          <input type="number" min=${spCtl.min} max=${spCtl.max} value=${target}
            onInput=${(e) => setTarget(e.target.value)} />
          <button class="btn sm primary" disabled=${busy === "setpoint"}
            onClick=${() => cmd(spCtl, Number(target), "setpoint")}>Set</button>
          <span class="note">${spCtl.min}–${spCtl.max}${u}${spNow != null ? ` · now ${spNow}${u}` : ""}</span>
        </div>`}
      ${rgCtl && html`
        <div class="field"><label>${rgCtl.label}</label>
          <div class="controls">
            ${rgCtl.options.map((o) => html`
              <button class="btn sm ${rgNow === o.value ? "primary" : ""}" disabled=${busy === "ranged"}
                title=${o.value} onClick=${() => cmd(rgCtl, o.value, "ranged")}>${o.label}</button>`)}
          </div>
          ${rgNow != null && html`<span class="note">now: ${rgNow}</span>`}
        </div>`}
      ${modeCtl && html`
        <div class="field"><label>${modeCtl.label}</label>
          <div class="controls">
            ${modeCtl.options.map((o) => html`
              <button class="btn sm ${modeNow === o.value ? "primary" : ""}" disabled=${busy === "mode"}
                title=${o.value} onClick=${() => cmd(modeCtl, o.value, "mode")}>${o.label}</button>`)}
          </div>
          ${modeLabel != null && html`<span class="note">now: ${modeLabel}</span>`}
        </div>`}
      ${indCtl && html`
        <div class="field"><label>${indCtl.label}</label>
          <div class="controls">
            <button class="btn sm" disabled=${busy === "indicator"}
              onClick=${() => cmd(indCtl, true, "indicator")}>On</button>
            <button class="btn sm" disabled=${busy === "indicator"}
              onClick=${() => cmd(indCtl, false, "indicator")}>Off</button>
          </div>
          ${indNow != null && html`<span class="note">now: ${indNow ? "on" : "off"}</span>`}
        </div>`}
      ${err && html`<span class="err">${err}</span>`}
      <div class="controls"><button class="btn sm ghost" onClick=${() => setOpen(false)}>Close</button></div>
    </div>`;
}

// ── device card ──────────────────────────────────────────────────────────────
// control-metric display map (the loop's input reading): RH for dehumidifiers, air quality for purifiers.
const CTRL_METRIC = {
  humidity_pct: { unit: "%", label: "control RH", round: 1 },
  pm25_ugm3: { unit: "µg/m³", label: "PM2.5", round: 0 },
  aqi: { unit: "", label: "AQI", round: 0 },
};

// Selectable air-quality control sources for threshold_ranged devices. EXPANDABLE: add an entry here
// (+ server _ALLOWED_CONTROL_METRICS + writer._UNITS) to offer a new metric. `cuts` = sensible default
// band cutoffs in that metric's units (speeds stay 1..N); switching metric resets cutoffs to these.
const AIR_QUALITY_METRICS = [
  { key: "pm25_ugm3", label: "PM2.5", unit: "µg/m³", cuts: [12, 35, 55] },
  { key: "aqi", label: "AQI", unit: "", cuts: [2, 3, 4] },
];

// automation editor for threshold_ranged devices (purifier): pick the air-quality source + edit the
// sensor->speed band cutoffs + enable/disable.
function RangedSettings({ vm, isAdmin, onChange, onNeedAdmin }) {
  const c = vm.control || {};
  const bands = c.bands || [];
  const speeds = bands.map((b) => b.level);
  const [open, setOpen] = useState(false);
  const [enabled, setEnabled] = useState(c.enabled !== false);
  const [metric, setMetric] = useState(c.metric || "pm25_ugm3");
  const [cuts, setCuts] = useState(bands.filter((b) => b.max != null).map((b) => b.max));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [flash, setFlash] = useState("");

  const sel = AIR_QUALITY_METRICS.find((m) => m.key === metric) || AIR_QUALITY_METRICS[0];
  // switching the control source resets the cutoffs to that metric's defaults (PM2.5 µg/m³ ≠ AQI 1-5).
  const changeMetric = (key) => {
    setMetric(key);
    const m = AIR_QUALITY_METRICS.find((x) => x.key === key);
    if (m) setCuts(m.cuts.slice());
  };
  const setCut = (i, v) => setCuts((cs) => cs.map((x, j) => (j === i ? v : x)));
  const issue = () => {
    for (let i = 0; i < cuts.length; i++) {
      if (cuts[i] === "" || !Number.isFinite(Number(cuts[i]))) return "enter all thresholds";
      if (i && Number(cuts[i]) <= Number(cuts[i - 1])) return "thresholds must increase";
    }
    return "";
  };
  const save = async () => {
    if (!isAdmin) return onNeedAdmin();
    const m = issue(); if (m) { setErr(m); setFlash(""); return; }
    const newBands = cuts.map((mx, i) => ({ max: Number(mx), level: speeds[i] }))
      .concat([{ max: null, level: speeds[speeds.length - 1] }]);
    setBusy(true); setErr(""); setFlash("");
    try {
      await adminSend("PUT", `/control/${vm.device_id}/policy`,
        { enabled, control: { strategy: "threshold_ranged", metric, bands: newBands } });
      setFlash("saved"); await onChange();
    } catch (e) { setErr(String(e.message)); }
    setBusy(false);
  };

  if (!open) {
    return html`<div class="settings"><button class="btn sm ghost"
        onClick=${() => setOpen(true)}>⚙ Automation</button></div>`;
  }
  return html`
    <div class="settings">
      <div class="divider"></div>
      <label class="switch">
        <input type="checkbox" checked=${enabled} onChange=${(e) => setEnabled(e.target.checked)} />
        Automation enabled (fan speed follows air quality)
      </label>
      <div class="field"><label>Control source</label>
        <select value=${metric} onChange=${(e) => changeMetric(e.target.value)}>
          ${AIR_QUALITY_METRICS.map((m) => html`<option value=${m.key}>${m.label}${m.unit ? ` (${m.unit})` : ""}</option>`)}
        </select></div>
      <div class="field"><label>Fan speed by ${sel.label}</label>
        ${cuts.map((v, i) => html`
          <div class="controls" key=${i}>
            <span class="note">Speed ${speeds[i]} below</span>
            <input type="number" value=${v} onInput=${(e) => setCut(i, e.target.value)} />
            <span class="note">${sel.unit || sel.label}</span>
          </div>`)}
        <p class="note">Speed ${speeds[speeds.length - 1]} when above ${cuts.length ? cuts[cuts.length - 1] : "—"} ${sel.unit}.</p>
      </div>
      ${issue() && html`<span class="err sm">⚠ ${issue()}</span>`}
      <div class="controls">
        <button class="btn sm primary" disabled=${busy || !!issue()} onClick=${save}>Save</button>
        <button class="btn sm ghost" disabled=${busy} onClick=${() => setOpen(false)}>Close</button>
        ${flash && html`<span class="ok-flash">${flash}</span>`}
        ${err && html`<span class="err">${err}</span>`}
      </div>
    </div>`;
}

function DeviceCard({ vm, sensors, isAdmin, onChange, onNeedAdmin, onEdit, onClose }) {
  const running = vm.running;
  const s = vm.sensor, o = vm.onboard, d = vm.last_decision, act = vm.actuator || {};
  const ageStale = vm.health === "stale";   // the BFF already derives staleness; mirror it on the age
  const cm = vm.control.metric || "humidity_pct";
  const CM = CTRL_METRIC[cm] || { unit: "", label: cm, round: 1 };
  const ranged = vm.control.strategy === "threshold_ranged";
  const cval = s ? (s[cm] != null ? s[cm] : s.value) : null;
  const fmtC = (v) => (v == null ? "—" : (CM.round ? round1(v) : Math.round(v)) + CM.unit);
  return html`
    <div class="card health-${vm.health}">
      <div class="card-head">
        <h2 style=${onClose ? "cursor:pointer" : ""} onClick=${onClose || undefined}>${dispName(vm)}${onClose ? html` <span class="chev">▾</span>` : ""}</h2>
        ${vm.room && html`<span class="sensor-area">${vm.room}</span>`}
        <span class="badge health-${vm.health}">${vm.health}</span>
        <button class="btn sm ghost edit-btn" onClick=${() => onEdit(vm)}>✎</button>
        ${onClose && html`<button class="btn sm ghost close-btn" title="collapse" onClick=${onClose}>✕</button>`}
      </div>
      <div class="state-row">
        <span class="pill ${running ? "on" : "off"}">${running == null ? "?" : running ? "RUNNING" : "IDLE"}</span>
        ${vm.control.enabled === false && html`<span class="note">automation off</span>`}
        ${(() => {
          const sa = vm.scene_active;
          if (!sa || sa.scene === "Home" || !sa.patch) return null;
          const what = sa.off ? "parked" : "relaxed";
          return html`<span class="scene-chip" title="Active scene affecting this device"
            >${SCENE_ICON[sa.scene] || ""} ${sa.scene}: ${what}</span>`;
        })()}
      </div>
      <div class="readings">
        <div class="reading">
          <div class="v">${fmtC(cval)}</div>
          <div class="k">${CM.label} · <span class=${ageStale ? "age-stale" : "age-fresh"}>${
            s ? fmtAge(s.age_s) : "no data"}</span></div>
          <div class="k">from ${vm.control.source_sensor || "—"}</div>
        </div>
        ${ranged ? html`
          <div class="reading">
            <div class="v">${act.fan_on === 0 ? "off" : (act.fan_speed != null ? "speed " + Math.round(act.fan_speed) : "—")}</div>
            <div class="k">fan</div>
          </div>
          <div class="reading muted">
            <div class="v">auto</div>
            <div class="k">speed ↔ ${CM.label}</div>
          </div>` : html`
          <div class="reading muted">
            <div class="v">${o ? round1(o.humidity_pct) + "%" : "—"}</div>
            <div class="k">onboard (not used)</div>
          </div>
          <div class="reading muted">
            <div class="v">${vm.control.on_above ?? "—"}/${vm.control.off_below ?? "—"}</div>
            <div class="k">on/off thresholds</div>
          </div>`}
      </div>
      ${d && html`<div class="reason">${d.source}: ${d.reason}</div>`}
      <${DecisionHistory} decisions=${vm.recent_decisions} />
      <${OverrideControls} vm=${vm} isAdmin=${isAdmin}
        onChange=${onChange} onNeedAdmin=${onNeedAdmin} />
      <${ManualControl} vm=${vm} isAdmin=${isAdmin} onChange=${onChange} onNeedAdmin=${onNeedAdmin} />
      ${ranged
        ? html`<${RangedSettings} vm=${vm} isAdmin=${isAdmin} onChange=${onChange} onNeedAdmin=${onNeedAdmin} />`
        : html`<${SettingsPanel} vm=${vm} sensors=${sensors} isAdmin=${isAdmin}
            onChange=${onChange} onNeedAdmin=${onNeedAdmin} />`}
    </div>`;
}

// minimized actuator — one compact grid cell mirroring SensorChip. Click to expand into the full
// DeviceCard (controls + history + settings). Shows name/room, run state, and the control reading.
function ActuatorChip({ vm, onOpen, onEdit }) {
  const running = vm.running;
  const cm = vm.control.metric || "humidity_pct";
  const CM = CTRL_METRIC[cm] || { unit: "", label: cm, round: 1 };
  const s = vm.sensor;
  const cval = s ? (s[cm] != null ? s[cm] : s.value) : null;
  const fmtC = (v) => (v == null ? "—" : (CM.round ? round1(v) : Math.round(v)) + CM.unit);
  return html`
    <div class="sensor actuator-chip" onClick=${onOpen}>
      ${onEdit && html`<button class="btn sm ghost edit-btn"
        onClick=${(e) => { e.stopPropagation(); onEdit(vm); }}>✎</button>`}
      <div class="sensor-name">${dispName(vm)} <span class="chev">▸</span></div>
      <div class="sensor-area">${vm.room || dispRoom(vm)}</div>
      <div class="sensor-vals">
        <span class="pill ${running ? "on" : "off"}">${running == null ? "?" : running ? "RUNNING" : "IDLE"}</span>
        ${cval != null && html`<span class="sv"><b>${fmtC(cval)}</b> ${CM.label}</span>`}
      </div>
      <div class="sensor-meta">
        <span class="badge health-${vm.health}">${vm.health}</span>
        ${vm.control.enabled === false && html` · <span class="note">auto off</span>`}
      </div>
    </div>`;
}

// Automations section: actuators render as compact tiles by default, expanding to the full DeviceCard
// on click (mirrors the Sensors chip→expanded pattern). Renders nothing when there are no actuators.
function Actuators({ devices, sensors, isAdmin, onEdit, onChange, onNeedAdmin }) {
  const [expanded, setExpanded] = useState(new Set());
  if (!devices || devices.length === 0) return null;
  const open = (id) => setExpanded((p) => new Set(p).add(id));
  const close = (id) => setExpanded((p) => { const n = new Set(p); n.delete(id); return n; });
  const mins = devices.filter((v) => !expanded.has(v.device_id));
  const exps = devices.filter((v) => expanded.has(v.device_id));
  return html`
    <h2 class="section">Automations</h2>
    ${mins.length > 0 && html`<div class="sensor-grid">
      ${mins.map((vm) => html`<${ActuatorChip} key=${vm.device_id} vm=${vm}
        onOpen=${() => open(vm.device_id)} onEdit=${onEdit} />`)}
    </div>`}
    ${exps.map((vm) => html`<${DeviceCard} key=${vm.device_id} vm=${vm} sensors=${sensors}
      isAdmin=${isAdmin} onChange=${onChange} onNeedAdmin=${onNeedAdmin} onEdit=${onEdit}
      onClose=${() => close(vm.device_id)} />`)}`;
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
  { key: "dewpoint_c", unit: "°C", color: "#22d3ee", label: "Dew point" },
  { key: "air_quality", unit: "AQ", color: "#4ade80", label: "Air Quality" },  // gas index (0-100, higher=cleaner); "AQ" identifies the bare number on the map glance
  { key: "co2_ppm", unit: "ppm", color: "#fbbf24", label: "CO₂" },
  { key: "radon_bqm3", unit: "Bq", color: "#a78bfa", label: "Radon" },
  { key: "pressure_hpa", unit: "hPa", color: "#34d399", label: "Pressure" },
  { key: "pm25_ugm3", unit: "µg/m³", color: "#fb7185", label: "PM2.5" },
  { key: "aqi", unit: "", color: "#fbbf24", label: "AQI" },
  { key: "voc_index", unit: "VOC", color: "#34d399", label: "VOC Index" },
  { key: "voc_raw", unit: "", color: "#6ee7b7", label: "VOC (raw)" },
  { key: "eco2", unit: "ppm", color: "#f59e0b", label: "eCO₂" },
  { key: "tvoc", unit: "ppb", color: "#a3e635", label: "TVOC" },
  { key: "gas_ohm", unit: "Ω", color: "#2dd4bf", label: "Gas resistance" },
  { key: "power_w", unit: "W", color: "#f59e0b", label: "Power" },
  { key: "energy_today_kwh", unit: "kWh", color: "#eab308", label: "Energy today" },
];

// Unified air-quality band → color (ADR-0035), shared by the map badge + sensor cards + legend. Keyed by the
// stable band int (5 Excellent → 1 Very Poor). Palette = the familiar EPA-AQI language (green→yellow→orange→
// red→purple) so the colors read at a glance; labels come from the server legend (band_legend()).
const AQ_BAND_COLOR = { 5: "#00e400", 4: "#ffff00", 3: "#ff7e00", 2: "#ff0000", 1: "#8f3f97" };

// shared value row (temp respects the °F/°C pref). `aq` = the node's air_quality_report (gas nodes), if any.
function SensorVals({ m, unit, aq }) {
  return html`<div class="sensor-vals">
    ${m.temperature_c != null && html`<span class="sv"><b>${round1(convT(m.temperature_c, unit))}°</b>${unit}</span>`}
    ${m.humidity_pct != null && html`<span class="sv"><b>${round1(m.humidity_pct)}</b>%RH</span>`}
    ${m.dewpoint_c != null && html`<span class="sv"><b>${round1(convT(m.dewpoint_c, unit))}°</b>${unit} Dew</span>`}
    ${m.co2_ppm != null && html`<span class="sv"><b>${Math.round(m.co2_ppm)}</b>ppm</span>`}
    ${m.radon_bqm3 != null && html`<span class="sv"><b>${Math.round(m.radon_bqm3)}</b>Bq</span>`}
    ${m.pressure_hpa != null && html`<span class="sv"><b>${Math.round(m.pressure_hpa)}</b>hPa</span>`}
    ${m.pm25_ugm3 != null && html`<span class="sv"><b>${Math.round(m.pm25_ugm3)}</b>µg/m³</span>`}
    ${m.aqi != null && html`<span class="sv"><b>${m.aqi}</b>AQI</span>`}
    ${(aq && aq.air_quality_band) ? html`<span class="sv" title=${aq.explanation || ""}>
        <b style=${`color:${AQ_BAND_COLOR[aq.air_quality_band] || "#94a3b8"}`}>${aq.air_quality_band_label}</b>${aq.air_quality_basis === "relative" ? " ~" : ""} AQ</span>`
      : (m.air_quality != null && html`<span class="sv"><b>${Math.round(m.air_quality)}</b> AQ</span>`)}
    ${m.voc_index != null && html`<span class="sv"><b>${m.voc_index}</b> VOC</span>`}
    ${m.power_w != null && html`<span class="sv"><b>${Math.round(m.power_w)}</b>W</span>`}
    ${m.energy_today_kwh != null && html`<span class="sv"><b>${round1(m.energy_today_kwh)}</b>kWh today</span>`}
    ${m.relay_on != null && html`<span class="sv"><b>${m.relay_on ? "on" : "off"}</b> plug</span>`}
    ${m.fan_speed != null && html`<span class="sv"><b>${m.fan_on === 0 ? "off" : m.fan_speed}</b> fan</span>`}
    ${m.filter_life_pct != null && html`<span class="sv"><b>${Math.round(m.filter_life_pct)}</b>% filter${m.filter_low ? " ⚠️" : ""}</span>`}
  </div>`;
}

// minimized preview — one compact grid cell. Click to expand (handled by the parent).
function SensorChip({ s, onOpen, onEdit }) {
  const m = s.metrics || {};
  const unit = useTemp();
  const stale = s.age_s != null && s.age_s > 1800;             // 30m: meters report often
  return html`
    <div class="sensor" onClick=${onOpen}>
      ${onEdit && html`<button class="btn sm ghost edit-btn"
        onClick=${(e) => { e.stopPropagation(); onEdit(s); }}>✎</button>`}
      <div class="sensor-name">${dispName(s)} <span class="chev">▸</span></div>
      <div class="sensor-area">${dispRoom(s)}</div>
      <${SensorVals} m=${m} unit=${unit} aq=${s.air_quality_report} />
      <div class="sensor-meta">
        <span class=${stale ? "age-stale" : "age-fresh"}>${fmtAge(s.age_s)}</span>
        ${m.battery_pct != null && html` · 🔋 ${Math.round(m.battery_pct)}%`}
      </div>
    </div>`;
}

// On-demand battery refresh (v20-battfix): asks the server to open a brief mesh-wide active-scan window
// so this meter's battery% updates within ~20s. The reading itself refreshes via the normal 5s poll.
function BatteryRefresh({ deviceId }) {
  const [st, setSt] = useState("");           // "" | busy | ok | err
  const go = async (e) => {
    e.stopPropagation();
    setSt("busy");
    try {
      const r = await fetch("/control/battery/refresh", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ device_id: deviceId }),
      });
      setSt(r.ok ? "ok" : "err");
    } catch { setSt("err"); }
    setTimeout(() => setSt(""), 9000);
  };
  const label = st === "busy" ? "🔋 refreshing…" : st === "ok" ? "🔋 refreshing ✓"
              : st === "err" ? "🔋 unavailable" : "🔋 refresh";
  return html`<button class="btn sm ghost batt-refresh" disabled=${st === "busy"} onClick=${go}
    title="Refresh battery levels now (brief mesh scan, ~20s)">${label}</button>`;
}

// expanded — full width, below the grid, charts over the shared range. Click the header to collapse.
function ExpandedSensor({ s, range, isAdmin, onEdit, onClose }) {
  const m = s.metrics || {};
  const unit = useTemp();
  const [series, setSeries] = useState(null);
  const [err, setErr] = useState("");
  // Shared UI spec (ADR-0019): the server authors the graphable-metric list (label/unit/color).
  // Fall back to the baked-in catalog only if the server field is absent (old server / offline).
  const graphList = s.graphs || GRAPHABLE.filter((g) => m[g.key] != null);
  useEffect(() => {
    let alive = true; setSeries(null); setErr("");
    (async () => {
      try {
        const keys = graphList.map((g) => g.key);
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
        <button class="btn sm ghost close-btn" title="collapse" onClick=${onClose}>✕</button>
      </div>
      <${SensorVals} m=${m} unit=${unit} aq=${s.air_quality_report} />
      ${(m.battery_pct != null || /meter|switchbot|aranet/i.test(s.device_type || "")) && html`
        <div class="batt-row" style="margin:2px 2px 8px"><${BatteryRefresh} deviceId=${s.device_id} /></div>`}
      ${s.air_quality_report && s.air_quality_report.explanation && html`<div class="aq-explain"
        style="font-size:12px;color:#9fb0c3;margin:2px 2px 8px;line-height:1.4">${s.air_quality_report.explanation}</div>`}
      <div class="charts">
        ${err && html`<div class="err">${err}</div>`}
        ${series == null && !err && html`<div class="note">loading…</div>`}
        ${series && graphList.filter((g) => series[g.key]).map((g) => html`
          <div class="chart-block" key=${g.key}>
            <div class="chart-label">${g.label}</div>
            <${AdaptiveChart} traces=${[{ label: g.label, color: g.color, unit: g.unit, metric: g.key, points: series[g.key] }]} />
          </div>`)}
      </div>
    </div>`;
}

function Sensors({ sensors, isAdmin, onEdit, onChange, roomFilter, onClearRoom }) {
  const [expanded, setExpanded] = useState(new Set());        // device_ids currently expanded
  const [range, setRange] = useState(() => computeRange(24, { start: "", end: "" }));
  const [managed, setManaged] = useState(null);               // null=not loaded; {hidden:[], retired:[]}
  if (sensors == null) return null;
  if (sensors.length === 0) return html`<p class="note">No sensor readings yet.</p>`;
  // room-zoom: when a room is picked on the house map, show only that room's sensors
  const all = roomFilter ? sensors.filter((s) => s.room === roomFilter.id) : sensors;
  const open = (id) => setExpanded((p) => new Set(p).add(id));
  const close = (id) => setExpanded((p) => { const n = new Set(p); n.delete(id); return n; });
  const mins = all.filter((s) => !expanded.has(s.device_id));
  const exps = all.filter((s) => expanded.has(s.device_id));

  const loadManaged = async () => {
    try {
      const d = await getJSON("/api/v1/devices/meta");
      const rows = Object.entries(d.meta || {}).map(([id, m]) => ({ device_id: id, ...m }));
      setManaged({ hidden: rows.filter((m) => m.hidden && !m.retired),   // retired shows only in its list
                   retired: rows.filter((m) => m.retired) });
    } catch { setManaged({ hidden: [], retired: [] }); }
  };
  const restore = async (id, field) => {                  // field = "hidden" | "retired"
    try { await adminSend("PUT", `/api/v1/devices/${id}/meta`, { [field]: false }); } catch {}
    await onChange(); loadManaged();
  };
  // Render one restore-list (hidden or retired) as tap-to-restore chips. Kept FLAT (single map, no nested
  // ternary-in-template) — the previous inline nesting broke the browser's module parser at load.
  const restoreList = (label, rows, field) =>
    rows.length === 0
      ? html`<span class="note">no ${label} devices</span>`
      : html`<span class="note">${label} — tap to restore:</span> ${rows.map((h) => html`
          <span class=${"trace-chip" + (field === "retired" ? " retired" : "")}
            onClick=${() => restore(h.device_id, field)}>${h.name || prettyName(h.device_id)} ↺</span>`)}`;

  return html`
    <div class="sensors-wrap">
      <h2 class="section">
        ${roomFilter ? html`${roomFilter.name} <button class="btn sm ghost" style="margin-left:8px"
          onClick=${onClearRoom}>← All rooms</button>` : "Sensors"}
      </h2>
      ${roomFilter && all.length === 0 && html`<p class="note">No sensors in this room.</p>`}
      <div class="sensor-grid">
        ${mins.map((s) => html`<${SensorChip} key=${s.device_id} s=${s}
          onOpen=${() => open(s.device_id)} onEdit=${onEdit} />`)}
      </div>
      ${exps.length > 0 && html`
        <div class="expanded-list">
          <${RangeControl} onRange=${setRange} />
          ${exps.map((s) => html`<${ExpandedSensor} key=${s.device_id} s=${s} range=${range}
            isAdmin=${isAdmin} onEdit=${onEdit} onClose=${() => close(s.device_id)} />`)}
        </div>`}
      ${isAdmin && html`<div class="hidden-ctl">
        ${managed == null
          ? html`<button class="btn sm ghost" onClick=${loadManaged}>manage hidden / retired</button>`
          : html`<div class="mgmt-row">${restoreList("hidden", managed.hidden, "hidden")}</div>
              <div class="mgmt-row">${restoreList("retired", managed.retired, "retired")}</div>`}
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

// ── device edit (R8 friendly name / room / hide + display calibration) ───────
// One-line summary + collapsible detail for a maintenance preview/job result.
function MaintResult({ res }) {
  if (!res) return null;
  let line = "", cls = "note";
  if (res.status === "preview") {
    const p = (res.report && res.report.plan && res.report.plan[0]) || {};
    line = `Preview (no changes made): ${p.old_id || ""}${p.new_id && p.new_id !== p.old_id ? " → " + p.new_id : ""}` +
           `${p.new_area && p.new_area !== p.old_area ? `  ·  ${p.old_area || "?"} → ${p.new_area}` : ""}`;
  } else if (res.status === "job") {
    const j = res.job || {};
    const r = j.report || {};
    if (j.status === "done") { cls = "note ok"; line = `✓ Applied. ${r.restart || ""}, ${r.sweep_passes ?? 0} sweep(s); verify ${r.clean ? "clean" : "…"}.`; }
    else if (j.status === "failed") { cls = "err"; line = `Applied but verify FAILED — stragglers remain. See detail + docs/runbook-dataset-restore.md.`; }
    else if (j.status === "error") { cls = "err"; line = `Error: ${j.error || "unknown"} (no/partial mutation; backups were written first).`; }
    else { cls = "note"; line = `Still running…`; }
  }
  return html`<div class=${cls}>${line}
    <details><summary class="note">detail</summary>
      <pre class="mono">${JSON.stringify(res.report || res.job || res, null, 2)}</pre></details></div>`;
}

// Recover is LIVE (2026-07-26): the on-device SwitchBot meter_pro history decode is fixed — the metadata
// type flipped 0x69->0x6a on current meter firmware (v23-mphist); the node windows back from the write
// pointer and the server ingest anchors + inserts (proven end-to-end on meter_pro_h_bed: newest recovered
// sample matched the live reading). A frozen-buffer guard (edge_history.py) refuses dead meters whose
// pointer never advances, so a stale unit (e.g. meter_pro_c_office) can't inject reanchored-old data.
const RECOVER_ENABLED = true;

// Render the history checker's gap report: a clean "no gaps" line, or the list of holes + whether
// they're recoverable (the device has an on-board buffer we can pull).
function HistoryReport({ hist }) {
  if (!hist) return null;
  const gaps = hist.gaps || [];
  const fmt = (iso) => {
    if (!iso) return "—";
    const d = new Date(iso), p = (n) => String(n).padStart(2, "0");
    return `${d.getMonth() + 1}/${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}`;
  };
  const dur = (m) => (m >= 60 ? `${(m / 60).toFixed(1)}h` : `${Math.round(m)}m`);
  if (gaps.length === 0) {
    return html`<p class="note ok">✅ No gaps over ${hist.min_gap_min}min in the last ${hist.lookback_days} days
      (${hist.reading_count} readings). Last reading ${fmt(hist.last_reading)}.</p>`;
  }
  return html`
    <div class="note">
      <p>⚠️ ${gaps.length} gap${gaps.length > 1 ? "s" : ""} in the last ${hist.lookback_days} days
        (biggest ${dur(hist.biggest_gap_min)}). ${hist.recoverable
          ? html`Recoverable via <b>${hist.recover_via}</b> — its on-board log can be pulled to backfill.`
          : html`<b>Not recoverable</b> — no on-device buffer for this sensor type.`}</p>
      <ul class="gap-list">
        ${gaps.map((g) => html`<li class="mono-id">${fmt(g.start)} → ${fmt(g.end)} · <b>${dur(g.gap_min)}</b></li>`)}
      </ul>
    </div>`;
}

function DeviceMetaModal({ device, areas, onClose, onSaved }) {
  const [name, setName] = useState(device.name || "");
  const [hidden, setHidden] = useState(!!device.hidden);
  const [retired, setRetired] = useState(!!device.retired);
  const [offsets, setOffsets] = useState({ ...(device.offsets || {}) });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  // ── Entity-plane maintenance (canonical area move / id rename) — distinct from the display overlay above.
  const [relArea, setRelArea] = useState(device.area || "");
  const [relMode, setRelMode] = useState("");          // "" = force an explicit pick (restamp|forward)
  const [newId, setNewId] = useState("");
  const [maBusy, setMaBusy] = useState(false);          // one maintenance op at a time
  const [maErr, setMaErr] = useState("");
  const [maRes, setMaRes] = useState(null);
  // ── History: gap check + on-demand recovery (reuses the nightly gap-watcher's scan + backfill) ──
  const [hcBusy, setHcBusy] = useState(false);
  const [hist, setHist] = useState(null);               // last gap report from the checker
  const [hrBusy, setHrBusy] = useState(false);
  const [hrMsg, setHrMsg] = useState("");
  const [histErr, setHistErr] = useState("");
  const metrics = GRAPHABLE.filter((g) => device.metrics && device.metrics[g.key] != null);
  const id = encodeURIComponent(device.device_id);
  const save = async () => {
    setBusy(true); setErr("");
    try {
      await adminSend("PUT", `/api/v1/devices/${id}/meta`, { name, hidden, retired });
      for (const g of metrics) {               // PUT only the offsets that changed
        const v = Number(offsets[g.key] || 0);
        if (v !== Number((device.offsets || {})[g.key] || 0)) {
          await adminSend("PUT", `/api/v1/devices/${id}/calibration`, { metric: g.key, offset: v });
        }
      }
      onSaved(); onClose();
    } catch (e) { setErr(String(e.message)); setBusy(false); }
  };
  // Run a maintenance op: dry_run=true → synchronous preview; real → launch detached job then poll.
  const runMaint = async (path, body, dry) => {
    setMaBusy(true); setMaErr(""); setMaRes(null);
    try {
      const res = await adminSend("POST", path, { ...body, dry_run: dry });
      if (res.status === "preview") { setMaRes({ status: "preview", report: res.report }); return; }
      if (res.status === "launched") {
        let rec = null;
        for (let i = 0; i < 120; i++) {          // ~3 min ceiling (fleet restart + bounded sweep)
          await new Promise((r) => setTimeout(r, 1500));
          try { rec = await adminSend("GET", res.poll); } catch (e) { rec = { status: "running" }; }
          setMaRes({ status: "job", job: rec });
          if (rec && rec.status !== "running") break;
        }
        if (rec && rec.status === "done") onSaved();   // refresh dashboard after a clean apply
        return;
      }
      setMaRes({ status: "job", job: res });
    } catch (e) { setMaErr(String(e.message)); }
    finally { setMaBusy(false); }
  };
  // History checker (read-only gap scan) + recovery (fire the type-appropriate on-board history pull,
  // then re-check so the closed gap is the proof — matches how the recover endpoint is designed).
  const checkHistory = async () => {
    setHcBusy(true); setHistErr(""); setHrMsg("");
    try { setHist(await adminSend("GET", `/api/v1/devices/${id}/history/gaps`)); }
    catch (e) { setHistErr(String(e.message)); }
    finally { setHcBusy(false); }
  };
  const recoverHistory = async () => {
    setHrBusy(true); setHistErr(""); setHrMsg("");
    try {
      const res = await adminSend("POST", `/api/v1/devices/${id}/history/recover`, {});
      if (res.status === "no_route") { setHrMsg(res.note || "No recovery route for this device type."); return; }
      setHrMsg(`Recovery started via ${res.via}${res.node ? ` (${res.node})` : ""} — re-checking in 45s…`);
      await new Promise((r) => setTimeout(r, 45000));
      await checkHistory();
      setHrMsg(`Recovery via ${res.via} dispatched — gap list refreshed above. Re-check again if a hole remains.`);
    } catch (e) { setHistErr(String(e.message)); }
    finally { setHrBusy(false); }
  };
  const areaChanged = relArea.trim() && relArea.trim() !== (device.area || "");
  const idChanged = newId.trim() && newId.trim() !== device.device_id;
  return html`
    <div class="modal-bg" onClick=${onClose}>
      <div class="modal" onClick=${(e) => e.stopPropagation()}>
        <h3>Edit device</h3>
        <p class="note mono-id">${device.device_id} · ${prettyArea(device.area)}</p>

        <h4 class="scope-hd">Display <span class="scope-sub">· this device only, cosmetic</span></h4>
        <p class="note">Changes only what's shown on the dashboard — no stored data is touched.</p>
        <div class="field-block">
          <label>Display name</label>
          <input value=${name} placeholder=${`default: ${prettyName(device.device_id)}`}
            onInput=${(e) => setName(e.target.value)} />
        </div>
        <label class="switch"><input type="checkbox" checked=${hidden}
          onChange=${(e) => setHidden(e.target.checked)} /> Hide from dashboard (temporary)</label>
        <label class="switch"><input type="checkbox" checked=${retired}
          onChange=${(e) => setRetired(e.target.checked)} /> Retire (decommissioned — archives it)</label>
        ${retired && !device.retired && html`<p class="note">Retiring archives the device — history is kept,
          it's removed from the dashboard, and it's not expected to report again. Restore it later from the
          "retired" list.</p>`}
        ${metrics.length > 0 && html`
          <p class="note">Display calibration — added to shown values + graphs (control uses raw):</p>
          ${metrics.map((g) => html`
            <div class="field"><label>${g.label} offset</label>
              <input type="number" step="0.1" value=${offsets[g.key] ?? ""}
                onInput=${(e) => setOffsets({ ...offsets, [g.key]: e.target.value })} />
              <span class="note">${g.unit}</span>
            </div>`)}`}
        ${err && html`<div class="err">${err}</div>`}
        <div class="modal-actions">
          <button class="btn ghost" onClick=${onClose}>Cancel</button>
          <button class="btn primary" disabled=${busy} onClick=${save}>${busy ? "Saving…" : "Save"}</button>
        </div>

        <div class="divider"></div>
        <h4 class="scope-hd">History <span class="scope-sub">· stored data — check &amp; recover gaps</span></h4>
        <p class="note">Scans the last 3 days for windows with no readings (e.g. after a power outage) and
          flags whether the device buffers history on-board, so a gap could be backfilled once on-demand
          recovery is enabled.${RECOVER_ENABLED ? " Recover pulls that on-board log to fill the hole — idempotent, safe to re-run." : ""}</p>
        <div class="modal-actions" style=${{ justifyContent: "flex-start" }}>
          <button class="btn ghost" disabled=${hcBusy} onClick=${checkHistory}>${hcBusy ? "Checking…" : "Check history"}</button>
          ${RECOVER_ENABLED && hist && hist.recoverable && (hist.gaps || []).length > 0 && html`
            <button class="btn primary" disabled=${hrBusy} onClick=${recoverHistory}>${hrBusy ? "Recovering…" : "Recover history"}</button>`}
        </div>
        <${HistoryReport} hist=${hist} />
        ${hrMsg && html`<p class="note ok">${hrMsg}</p>`}
        ${histErr && html`<div class="err">${histErr}</div>`}

        <div class="divider"></div>
        <h4 class="scope-hd">Location &amp; identity <span class="scope-sub">· canonical — changes stored data</span></h4>
        <p class="note">These rewrite the registry + history and <b>restart the ingest fleet</b>, so they run
          as a background job (~20–40s). Preview first; a backup is written before any change.</p>

        <div class="field-block">
          <label>Relocate — move to room</label>
          ${areas && areas.length
            ? html`<select value=${relArea} onChange=${(e) => setRelArea(e.target.value)}>
                ${!areas.includes(relArea) && html`<option value=${relArea}>${relArea ? prettyArea(relArea) : "— select area —"}</option>`}
                ${areas.map((a) => html`<option value=${a}>${prettyArea(a)}${a === device.area ? " (current)" : ""}</option>`)}
              </select>`
            : html`<input value=${relArea} placeholder="area id (e.g. living_room)"
                onInput=${(e) => setRelArea(e.target.value)} />`}
          <div class="radio-row">
            <label class="switch"><input type="radio" name="relmode" checked=${relMode === "restamp"}
              onChange=${() => setRelMode("restamp")} /> Restamp history <span class="note">(fix a mislabel — the past follows the device)</span></label>
            <label class="switch"><input type="radio" name="relmode" checked=${relMode === "forward"}
              onChange=${() => setRelMode("forward")} /> Forward only <span class="note">(physical move — leave past history where it was)</span></label>
          </div>
          <div class="modal-actions">
            <button class="btn ghost" disabled=${maBusy || !areaChanged || !relMode}
              onClick=${() => runMaint(`/api/v1/devices/${id}/relocate`, { new_area: relArea.trim(), mode: relMode }, true)}>Preview</button>
            <button class="btn primary" disabled=${maBusy || !areaChanged || !relMode}
              onClick=${() => runMaint(`/api/v1/devices/${id}/relocate`, { new_area: relArea.trim(), mode: relMode }, false)}>${maBusy ? "Working…" : "Relocate"}</button>
          </div>
        </div>

        <details class="advanced">
          <summary>Advanced — rename internal id</summary>
          <div class="field-block danger">
            <label>Rename — device id</label>
            <p class="note">The device's permanent database identity. Rewrites it <b>everywhere</b> —
              history, rollups, registry, control policy + secret, and the .245 peer. Rarely needed; changing
              the display name above is usually what you want.</p>
            <input value=${newId} placeholder=${`new id (a–z, 0–9, _) — current: ${device.device_id}`}
              onInput=${(e) => setNewId(e.target.value)} />
            <div class="modal-actions">
              <button class="btn ghost" disabled=${maBusy || !idChanged}
                onClick=${() => runMaint(`/api/v1/devices/${id}/rename`, { new_id: newId.trim() }, true)}>Preview</button>
              <button class="btn danger" disabled=${maBusy || !idChanged}
                onClick=${() => runMaint(`/api/v1/devices/${id}/rename`, { new_id: newId.trim() }, false)}>${maBusy ? "Working…" : "Rename"}</button>
            </div>
          </div>
        </details>

        ${maErr && html`<div class="err">${maErr}</div>`}
        <${MaintResult} res=${maRes} />
      </div>
    </div>`;
}

const KNOWN_TRAITS = ["switchable", "ranged", "setpoint", "lockable", "positionable"];
// Sensor device_types offered in the Add-sensor dropdown (BLE-heard first, then edge/I2C). "Other…"
// reveals a free-text box so a brand-new model is still addable. Keep in step with the registry.
const SENSOR_TYPES = [
  "switchbot_meter_outdoor", "switchbot_meter_pro", "switchbot_meter_plus", "switchbot_meter",
  "aranet_radon_plus", "bme680_gas", "sgp40_gas", "sgp30_gas",
];

// Signal-strength glyph from RSSI (dBm): closer device → more bars. Held-to-the-hub is usually > -50.
function sigBars(rssi) {
  const n = rssi >= -50 ? 4 : rssi >= -65 ? 3 : rssi >= -80 ? 2 : 1;
  return "▁▂▄▆█".slice(0, 0) + ["▂··", "▂▄·", "▂▄▆", "▂▄█"][n - 1];
}

// After a sensor is registered we don't tell the user to restart anything: ha-scanner/ha-edge-mapper
// hot-reload devices.yaml on mtime (RegistryReloader), so the new device starts publishing within
// seconds. We prove it by polling its last reading — a live success/failure watch instead of a note.
function LiveConfirm({ deviceId, onDone }) {
  const [state, setState] = useState("waiting");   // waiting | live | slow
  const [readings, setReadings] = useState(null);
  useEffect(() => {
    let alive = true, tries = 0;
    const poll = async () => {
      tries += 1;
      try {
        const r = await fetch(`/devices/${deviceId}/last`);
        if (r.ok) {
          const d = await r.json();
          if (alive) { setReadings(d.readings || []); setState("live"); }
          return true;
        }
      } catch (e) { /* not reporting yet */ }
      if (tries >= 12 && alive) setState("slow");   // ~24s with no reading
      return false;
    };
    poll();
    const id = setInterval(async () => { if (await poll()) clearInterval(id); }, 2000);
    return () => { alive = false; clearInterval(id); };
  }, [deviceId]);
  const fmt = (r) => {
    if (r.metric === "temperature_c") return `${Math.round((r.value * 9 / 5 + 32) * 10) / 10}°F`;
    if (r.metric === "humidity_pct") return `${Math.round(r.value)}% RH`;
    if (r.metric === "battery_pct") return `🔋 ${Math.round(r.value)}%`;
    return `${r.metric} ${r.value}`;
  };
  return html`
    <p class="note ok">✅ Registered <b>${deviceId}</b> — hot-reloaded into the scanner (no restart).</p>
    ${state === "waiting" && html`<p class="note">⏳ Waiting for its first reading… keep it powered near the hub.</p>`}
    ${state === "live" && html`
      <p class="note ok">📡 It's live — reporting now:</p>
      <div class="cand-vals">${(readings || []).map((r) => html`<span>${fmt(r)}</span>`)}</div>`}
    ${state === "slow" && html`
      <p class="note">No reading yet after ~25s. Keep it powered and close. If it stays silent it may be
        out of range of this hub, or the scanner needs a restart (<code>systemctl restart ha-scanner</code>).</p>`}
    <div class="modal-actions">
      <button class="btn primary" onClick=${onDone}>Done</button>
    </div>`;
}

function AddDeviceModal({ onClose, onSaved }) {
  const [kind, setKind] = useState("sensor");        // sensor | actuator
  const [mac, setMac] = useState("");
  const [deviceId, setDeviceId] = useState("");
  const [deviceType, setDeviceType] = useState("");
  const [node, setNode] = useState("");
  const [area, setArea] = useState("");
  const [traits, setTraits] = useState([]);
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [done, setDone] = useState(null);
  const [cands, setCands] = useState(null);          // null = not fetched yet; [] = heard nothing
  const [scanErr, setScanErr] = useState("");
  const [edgeCands, setEdgeCands] = useState(null);  // ADR-0036: online-but-unassigned edge NODES (standby hw)
  const [edgePick, setEdgePick] = useState(null);    // the standby node chosen for adoption
  const [edgeArea, setEdgeArea] = useState("");
  const [edgeName, setEdgeName] = useState("");       // optional rename-on-adopt (fired AFTER the relocate)
  const [edgeBusy, setEdgeBusy] = useState(false);
  const [edgeErr, setEdgeErr] = useState("");
  const [edgeDone, setEdgeDone] = useState(null);
  const [typeManual, setTypeManual] = useState(false);   // "Other…" chosen → free-text device_type
  const [areas, setAreas] = useState([]);                // canonical rooms {id,name} for the area dropdowns
  const [areaManual, setAreaManual] = useState(false);   // device-add "Other…" → free-text NEW area
  const [edgeAreaManual, setEdgeAreaManual] = useState(false);   // edge-adopt "Other…" → free-text NEW area
  const toggleTrait = (t) => setTraits((ts) => (ts.includes(t) ? ts.filter((x) => x !== t) : [...ts, t]));

  // Canonical rooms for the area pickers (ADR-0026) — a dropdown beats free-text (typos spawn phantom areas).
  useEffect(() => {
    getJSON("/api/v1/rooms")
      .then((d) => setAreas((d.rooms || []).map((r) => ({ id: r.id, name: r.name }))))
      .catch(() => setAreas([]));
  }, []);
  // Reusable area picker: dropdown of canonical rooms + "Other…" → free-text for a genuinely new room.
  const areaSelect = (val, setVal, manual, setManual, ph) => html`
    <select value=${manual ? "__other__" : (areas.some((a) => a.id === val) ? val : "")}
      onChange=${(e) => { const v = e.target.value;
        if (v === "__other__") { setManual(true); setVal(""); } else { setManual(false); setVal(v); } }}>
      <option value="" disabled>room / area…</option>
      ${areas.map((a) => html`<option value=${a.id}>${a.name}</option>`)}
      <option value="__other__">Other… (new room)</option>
    </select>
    ${manual && html`<input value=${val} placeholder=${ph}
      onInput=${(e) => setVal(e.target.value.trim())} />`}`;

  // Poll the discovery feed while adding a SENSOR — the "see the filtered-out data" half of onboarding.
  useEffect(() => {
    if (kind !== "sensor" || done) return;
    let alive = true;
    const tick = async () => {
      try {
        const r = await adminSend("GET", "/api/v1/discover");
        if (alive) { setCands(r.candidates || []); setEdgeCands(r.edge_nodes || []); setScanErr(""); }
      } catch (e) { if (alive) setScanErr(String(e.message)); }
    };
    tick();
    const id = setInterval(tick, 3000);
    return () => { alive = false; clearInterval(id); };
  }, [kind, done]);

  const pick = (c) => {
    setMac(c.mac);
    if (c.device_type) { setDeviceType(c.device_type); setTypeManual(!SENSOR_TYPES.includes(c.device_type)); }
    setErr("");
  };
  const onTypeSelect = (e) => {
    const v = e.target.value;
    if (v === "__other__") { setTypeManual(true); setDeviceType(""); }
    else { setTypeManual(false); setDeviceType(v); }
  };
  const typeSelVal = typeManual ? "__other__" : (SENSOR_TYPES.includes(deviceType) ? deviceType : "");
  const save = async () => {
    setBusy(true); setErr("");
    try {
      let r;
      if (kind === "sensor") {
        const body = { mac: mac.trim(), device_id: deviceId.trim(),
                       device_type: deviceType.trim(), area: area.trim() };
        if (notes.trim()) body.notes = notes.trim();
        r = await adminSend("POST", "/api/v1/devices", body);
      } else {
        const tr = {}; traits.forEach((t) => (tr[t] = {}));
        r = await adminSend("POST", "/api/v1/control-devices",
          { device_id: deviceId.trim(), node: node.trim(), area: area.trim(), traits: tr });
      }
      setDone(r);
    } catch (e) { setErr(String(e.message)); setBusy(false); }
  };

  // ADR-0036 intake: adopt a standby edge node into a room (relocate its dormant gas device → area).
  const adoptNode = async () => {
    if (!edgePick || !edgeArea.trim()) return;
    setEdgeBusy(true); setEdgeErr("");
    try {
      const r = await adminSend("POST", `/api/v1/edge-nodes/${edgePick.node}/intake`, { area: edgeArea.trim() });
      // Optional rename-on-adopt: the intake relocate is async/detached, so the rename is a SEPARATE
      // follow-up request (as the backend intends) — never chained server-side. Best-effort; if it races the
      // relocate you can still rename via the pencil. new_id must be [a-z0-9_]+.
      const nm = edgeName.trim();
      if (nm && r && r.device_id) {
        try { await adminSend("POST", `/api/v1/devices/${r.device_id}/rename`, { new_id: nm }); r.renamed_to = nm; }
        catch (e2) { r.rename_error = String(e2.message); }
      }
      setEdgeDone(r);
    } catch (e) { setEdgeErr(String(e.message)); }
    setEdgeBusy(false);
  };
  const edgeDiscover = html`
    <div class="discover">
      <div class="discover-hd"><span>Standby / unassigned hardware</span>
        <span class="note sm">${edgeCands === null ? "listening…" : "live · every 3s"}</span></div>
      ${edgeCands !== null && edgeCands.length === 0 && html`<p class="note sm">No unassigned edge nodes
        online. Power a standby node (C6/S3) on the house network — it self-announces and shows up here.</p>`}
      ${(edgeCands || []).map((n) => html`
        <button class="cand ${edgePick && edgePick.node === n.node ? "sel" : ""}"
          onClick=${() => { setEdgePick(n); setEdgeArea(""); setEdgeErr(""); setEdgeDone(null); }}>
          <span class="cand-main">
            <b>${n.node}</b>
            <span class="note sm mono">${n.chip || "?"} · ${(n.abilities || []).join(", ") || "sensor?"}${n.fw ? " · " + n.fw : ""}${n.known === false ? " · unmanifested" : ""}</span>
          </span>
          <span class="cand-vals">
            <span class="note sm">${n.age_s != null ? `seen ${Math.round(n.age_s)}s ago` : "online"}</span>
          </span>
        </button>`)}
      ${edgePick && html`
        <div class="edge-adopt">
          ${areaSelect(edgeArea, setEdgeArea, edgeAreaManual, setEdgeAreaManual, "new area slug — e.g. living_room")}
          <input value=${edgeName} placeholder="rename to (optional) — e.g. gas_office [a-z0-9_]"
            onInput=${(e) => setEdgeName(e.target.value.trim())} />
          <button class="btn primary sm" disabled=${edgeBusy || !edgeArea.trim()} onClick=${adoptNode}>
            ${edgeBusy ? "Adopting…" : `Adopt ${edgePick.node}${edgeArea ? " → " + edgeArea : ""}`}</button>
          ${edgeErr && html`<p class="err sm">${edgeErr}</p>`}
          ${edgeDone && html`<p class="note sm">✓ ${edgeDone.node} adopting into <b>${edgeDone.area}</b>
            (device ${edgeDone.renamed_to || edgeDone.device_id}). The fleet restarts to pick it up — give it a moment.
            ${edgeDone.rename_error && html`<br/><span class="err">rename deferred (${edgeDone.rename_error}) — use the pencil once it settles.</span>`}</p>`}
        </div>`}
    </div>`;

  const outdoorPicked = deviceType === "switchbot_meter_outdoor";
  const discover = html`
    <div class="discover">
      <div class="discover-hd"><span>Nearby unregistered devices</span>
        <span class="note sm">${cands === null ? "listening…" : "live · every 3s"}</span></div>
      ${scanErr && html`<p class="err sm">${scanErr}</p>`}
      ${cands === null ? html`<p class="note sm">Listening — power the new device on and hold it near the hub.</p>`
        : cands.length === 0 ? html`<p class="note sm">Nothing heard yet. Keep the device close; the scanner
            surfaces each one about once a minute.</p>`
        : cands.map((c) => html`
          <button class="cand ${c.mac === mac ? "sel" : ""}" onClick=${() => pick(c)}>
            <span class="cand-sig" title=${`${c.rssi_max} dBm`}>${sigBars(c.rssi_max)}</span>
            <span class="cand-main">
              <b>${c.device_type || c.brand}</b>
              <span class="note sm mono">${c.mac}</span>
            </span>
            <span class="cand-vals">
              ${c.temperature_f != null ? html`<span>${c.temperature_f}°F</span>` : ""}
              ${c.humidity_pct != null ? html`<span>${c.humidity_pct}% RH</span>` : ""}
              ${c.battery_pct != null ? html`<span>🔋${c.battery_pct}%</span>` : html`<span class="note sm">batt ?</span>`}
            </span>
          </button>`)}
    </div>`;

  const sensorFields = html`
    ${edgeDiscover}
    ${discover}
    <input value=${mac} placeholder="MAC — pick above, or type AA:BB:CC:DD:EE:FF"
      onInput=${(e) => setMac(e.target.value.toUpperCase())} />
    <select value=${typeSelVal} onChange=${onTypeSelect}>
      <option value="" disabled>device type…</option>
      ${SENSOR_TYPES.map((t) => html`<option value=${t}>${t}</option>`)}
      <option value="__other__">Other… (type manually)</option>
    </select>
    ${typeManual && html`<input value=${deviceType} placeholder="device_type — e.g. switchbot_meter_outdoor"
      onInput=${(e) => setDeviceType(e.target.value)} />`}
    ${outdoorPicked && html`<p class="note sm">Outdoor meters use a rotating BLE address — you're binding the
      MAC shown now; if it later rotates you'd re-add it.</p>`}
    <input value=${notes} placeholder="notes (optional)" onInput=${(e) => setNotes(e.target.value)} />`;
  const actuatorFields = html`
    <input value=${node} placeholder="node — the enrolled edge node, e.g. c6-bench"
      onInput=${(e) => setNode(e.target.value)} />
    <p class="note">Traits (ADR-0002):</p>
    <div class="traits">${KNOWN_TRAITS.map((t) => html`
      <label class="switch"><input type="checkbox" checked=${traits.includes(t)}
        onChange=${() => toggleTrait(t)} /> ${t}</label>`)}</div>`;
  const form = html`
    <div class="seg">
      <button class="btn sm ${kind === "sensor" ? "primary" : "ghost"}" onClick=${() => setKind("sensor")}>Sensor</button>
      <button class="btn sm ${kind === "actuator" ? "primary" : "ghost"}" onClick=${() => setKind("actuator")}>Actuator</button>
    </div>
    ${kind === "sensor" ? sensorFields : actuatorFields}
    <input value=${deviceId} placeholder=${`device_id slug — e.g. ${kind === "sensor" ? "meter_patio" : "lamp_office"}`}
      onInput=${(e) => setDeviceId(e.target.value)} />
    ${areaSelect(area, setArea, areaManual, setAreaManual, "new area slug — e.g. patio")}
    ${err && html`<div class="err">${err}</div>`}
    <div class="modal-actions">
      <button class="btn ghost" onClick=${onClose}>Cancel</button>
      <button class="btn primary" disabled=${busy} onClick=${save}>${busy ? "Adding…" : "Add"}</button>
    </div>`;
  return html`
    <div class="modal-bg" onClick=${onClose}>
      <div class="modal" onClick=${(e) => e.stopPropagation()}>
        <h3>Add a ${kind}</h3>
        <p class="note">${kind === "actuator"
          ? "Appends to control.yaml. The node must already be enrolled, or it won't be commandable."
          : "Pick a nearby device below (no pairing — the hub already hears it), name it, and give it a room."}</p>
        ${done ? html`<${LiveConfirm} deviceId=${done.device_id} onDone=${() => { onSaved(); onClose(); }} />` : form}
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
    // EVERYTHING in try/catch so a failure can never leave the dialog stuck on "Checking…".
    // VERIFY against the server before accepting — auth/check 200s only for a valid bearer.
    try {
      const tok = await deriveToken(pw);
      const r = await fetch("/control/auth/check", { headers: { Authorization: "Bearer " + tok } });
      if (r.ok) { setToken(tok); onUnlock(); onClose(); return; }
      setErr("Incorrect password");
    } catch (e) {
      setErr("Login error: " + (e && e.message ? e.message : String(e)));
    }
    setBusy(false);
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

// ── notifications toggle (Web Push) ──────────────────────────────────────────
// Subscribes this browser to background alert notifications. Payload-less: the SW fetches the alerts.
const SCENE_ICON = { Home: "🏠", Away: "🚪", Sleep: "🌙" };
function SceneSelector({ isAdmin, onNeedAdmin, onChange }) {
  // Whole-house scene (Home/Away/Sleep). Reads /api/v1/house (open); setting is admin-gated. The active
  // scene relaxes/parks each device per its policy `scenes` map — the effect lands on the next control tick.
  const [house, setHouse] = useState(null);     // {scene, scenes, set_ts}
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => getJSON("/api/v1/house").then(setHouse).catch(() => {}), []);
  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, [load]);
  if (!house || !(house.scenes || []).length) return null;
  const pick = async (scene) => {
    if (scene === house.scene || busy) return;
    if (!isAdmin) { onNeedAdmin && onNeedAdmin(); return; }
    setBusy(true);
    try {
      const r = await adminSend("POST", "/control/house/scene", { scene });
      setHouse((h) => ({ ...h, scene: r.scene, set_ts: r.set_ts }));
      onChange && onChange();
    } catch (e) { alert("Scene: " + e.message); }
    setBusy(false);
  };
  return html`<div class="scene-sel" title="Whole-house scene — relaxes or parks devices per their policy">
    ${house.scenes.map((s) => html`<button class="btn sm ${s === house.scene ? "scene-on" : "ghost"}"
        key=${s} disabled=${busy} onClick=${() => pick(s)} title=${s}>${SCENE_ICON[s] || ""} ${s}</button>`)}
  </div>`;
}

function NightMode({ isAdmin, onNeedAdmin }) {
  // Global LED night mode: turns every indicator-capable device's LED off during the window (on after).
  // Reads /api/v1/house (open); setting is admin-gated (PUT /control/house/night-mode).
  const [nm, setNm] = useState(null);          // {enabled, window}
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => getJSON("/api/v1/house")
    .then((h) => setNm(h.night_mode || { enabled: false, window: "22:00-07:00" })).catch(() => {}), []);
  useEffect(() => { load(); const t = setInterval(load, 30000); return () => clearInterval(t); }, [load]);
  if (!nm) return null;
  const save = async (next) => {
    if (!isAdmin) { onNeedAdmin && onNeedAdmin(); return; }
    setBusy(true);
    try { const r = await adminSend("PUT", "/control/house/night-mode", next); setNm(r.night_mode); }
    catch (e) { alert("Night mode: " + e.message); }
    setBusy(false);
  };
  // window is "HH:MM-HH:MM"; split into two native time pickers (touch-friendly — no typing).
  const [from, to] = (nm.window || "22:00-07:00").split("-");
  return html`<div class="scene-sel" title="LED night mode — turns device LEDs off during the window">
    <button class="btn sm ${nm.enabled ? "scene-on" : "ghost"}" disabled=${busy}
        onClick=${() => save({ enabled: !nm.enabled, window: nm.window })}>🌙 LEDs ${nm.enabled ? "On" : "Off"}</button>
    ${nm.enabled && html`<span class="nm-window">
      <input type="time" class="nm-time" value=${from} disabled=${busy} title="LEDs off from"
        onChange=${(e) => e.target.value && save({ enabled: true, window: `${e.target.value}-${to}` })} />
      <span class="note">–</span>
      <input type="time" class="nm-time" value=${to} disabled=${busy} title="LEDs off until"
        onChange=${(e) => e.target.value && save({ enabled: true, window: `${from}-${e.target.value}` })} />
    </span>`}
  </div>`;
}

function NotifyToggle() {
  const [state, setState] = useState("default");   // unsupported|denied|subscribed|default
  const [busy, setBusy] = useState(false);
  useEffect(() => { pushState().then(setState).catch(() => setState("unsupported")); }, []);
  if (!pushSupported() || state === "unsupported") return null;
  const onClick = async () => {
    setBusy(true);
    try { setState(state === "subscribed" ? await disablePush() : await enablePush()); }
    catch (e) { alert("Notifications: " + e.message); }
    setBusy(false);
  };
  const label = state === "subscribed" ? "🔔 On" : state === "denied" ? "🔕 Blocked" : "🔔 Off";
  const title = state === "denied"
    ? "Notifications blocked in browser settings"
    : state === "subscribed" ? "Background alerts on — tap to turn off" : "Enable background alert notifications";
  return html`<button class="btn sm ghost" disabled=${busy || state === "denied"}
      onClick=${onClick} title=${title}>${label}</button>`;
}

// Room placement editor (map arc 3) — drag each device to its spot within the selected room. Position
// is NORMALIZED to the room bounding box (0..1, x west→east, y north→south) so it survives geometry
// re-scaling and is reusable at any zoom (room-zoom AND, if desired, the house map). Saves to
// PUT /api/v1/devices/{id}/placement (admin-gated). The D1001 panel renders these read-only.
const clamp01 = (v) => Math.max(0, Math.min(1, v));
const round2 = (v) => Math.round(v * 100) / 100;

function RoomPlacementEditor({ room, isAdmin, onNeedAdmin, onSaved }) {
  const svgRef = useRef(null);
  const [pos, setPos] = useState({});      // device_id -> {x,y} normalized (authoritative during a drag)
  const [drag, setDrag] = useState(null);  // device_id being dragged
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");

  // seed local positions from the server placements whenever the selected room changes
  useEffect(() => {
    const p = {};
    for (const d of room.devices || []) {
      const pl = d.placement || {};
      if (typeof pl.x === "number" && typeof pl.y === "number") p[d.device_id] = { x: pl.x, y: pl.y };
    }
    setPos(p);
  }, [room.id]);

  const g = room.geometry;
  if (!g || !(g.poly || g.polys)) return null;             // only rooms with a polygon
  let mnx = 1e9, mny = 1e9, mxx = -1e9, mxy = -1e9;
  const grow = (x, y) => { mnx = Math.min(mnx, x); mny = Math.min(mny, y); mxx = Math.max(mxx, x); mxy = Math.max(mxy, y); };
  const rings = g.polys || [g.poly];
  rings.forEach((r) => r.forEach(([x, y]) => grow(x, y)));
  const W = mxx - mnx, H = mxy - mny;
  const R = Math.max(W, H) * 0.018, FS = Math.max(W, H) * 0.028;

  const toNorm = (e) => {
    const svg = svgRef.current, m = svg && svg.getScreenCTM();
    if (!m) return null;
    const pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY;
    const p = pt.matrixTransform(m.inverse());
    return { x: clamp01((p.x - mnx) / W), y: clamp01((p.y - mny) / H) };
  };
  const save = async (id, body) => {
    if (!isAdmin) { onNeedAdmin(); return; }
    setBusy(id); setErr("");
    try { await adminSend("PUT", `/api/v1/devices/${id}/placement`, body); onSaved && onSaved(); }
    catch (e) { setErr(`Couldn't save ${id}: ${e.message || e}`); }
    setBusy("");
  };
  const onMove = (e) => { if (!drag) return; const n = toNorm(e); if (n) setPos((s) => ({ ...s, [drag]: n })); };
  const onUp = () => { if (!drag) return; const id = drag; setDrag(null); const n = pos[id]; if (n) save(id, { x: round2(n.x), y: round2(n.y), anchor: "auto" }); };
  const startDrag = (id) => (e) => {
    if (!isAdmin) { onNeedAdmin(); return; }
    e.preventDefault();
    if (svgRef.current && svgRef.current.setPointerCapture) svgRef.current.setPointerCapture(e.pointerId);
    setDrag(id);
  };
  const placeAt = (id, x, y) => { setPos((s) => ({ ...s, [id]: { x, y } })); save(id, { x, y, anchor: "auto" }); };
  const clear = (id) => { setPos((s) => { const c = { ...s }; delete c[id]; return c; }); save(id, { x: null, y: null, anchor: "auto" }); };

  const devs = room.devices || [];
  const unplaced = devs.filter((d) => !pos[d.device_id]);
  const placedDevs = devs.filter((d) => pos[d.device_id]);

  return html`<div class="placement-editor">
    <h2 class="section">Placement — ${room.name}
      ${!isAdmin && html`<button class="btn sm" onClick=${onNeedAdmin}>🔒 unlock to edit</button>`}</h2>
    <p class="note">Drag a device to its spot in the room; positions save automatically. The wall panel
      shows these read-only. Positions are relative to the room, so they hold if the room is rescaled.</p>
    ${err && html`<p class="err sm">⚠ ${err}</p>`}
    <svg ref=${svgRef} viewBox="${mnx} ${mny} ${W} ${H}" preserveAspectRatio="xMidYMid meet"
         style="width:100%;max-height:62vh;display:block;touch-action:none;background:rgba(20,28,48,0.5);border-radius:8px"
         onPointerMove=${onMove} onPointerUp=${onUp} onPointerCancel=${onUp}>
      ${rings.map((ring) => html`<polygon points=${ring.map((p) => p.join(",")).join(" ")}
        fill="rgba(47,126,122,0.12)" stroke="#2f7e7a" stroke-width=4 stroke-linejoin="round" />`)}
      ${placedDevs.map((d) => {
        const p = pos[d.device_id], cx = mnx + p.x * W, cy = mny + p.y * H, on = drag === d.device_id;
        return html`<g style="cursor:grab" onPointerDown=${startDrag(d.device_id)}>
          <circle cx=${cx} cy=${cy} r=${R} fill=${on ? "#b4823c" : "#3d6e93"} stroke="#fff" stroke-width=2 />
          <text x=${cx} y=${cy - R - 4} text-anchor="middle" font-size=${FS} fill="currentColor">${d.name || d.device_id}</text>
        </g>`;
      })}
    </svg>
    ${unplaced.length > 0 && html`<div class="tray" style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:8px">
      <span class="note">Unplaced — tap to drop at center, then drag:</span>
      ${unplaced.map((d) => html`<button class="btn sm ghost" disabled=${busy === d.device_id}
        onClick=${() => (isAdmin ? placeAt(d.device_id, 0.5, 0.5) : onNeedAdmin())}>${d.name || d.device_id}</button>`)}
    </div>`}
    ${placedDevs.length > 0 && html`<div class="tray" style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:6px">
      <span class="note">Placed:</span>
      ${placedDevs.map((d) => html`<button class="btn sm ghost" title="remove placement"
        onClick=${() => clear(d.device_id)}>${d.name || d.device_id} ✕</button>`)}
    </div>`}
  </div>`;
}

// ── app shell ────────────────────────────────────────────────────────────────
// Fit a room name inside its polygon (SVG <text> has no native wrap): greedy word-wrap into lines
// that each fit maxW, and as a last resort squeeze an over-wide line with textLength. Returns
// [{ text, tl }] top-to-bottom — tl set only when that line still overflows and must be squeezed.
const LABEL_CHAR_W = 0.55;                       // rough glyph advance per char at 1em
function wrapLabel(name, maxW, fontSize) {
  const wordW = (s) => s.length * fontSize * LABEL_CHAR_W;
  const words = String(name).split(/\s+/).filter(Boolean);
  if (words.length <= 1 || wordW(name) <= maxW)  // single word, or already fits on one line
    return [{ text: name, tl: wordW(name) > maxW ? maxW : undefined }];
  const lines = [];
  let cur = words[0];
  for (let i = 1; i < words.length; i++) {
    const cand = cur + " " + words[i];
    if (wordW(cand) <= maxW) cur = cand;         // word still fits on the current line
    else { lines.push(cur); cur = words[i]; }    // overflow → start a new line
  }
  lines.push(cur);
  return lines.map((t) => ({ text: t, tl: wordW(t) > maxW ? maxW : undefined }));
}

// House map — the /api/v1/rooms room graph as an SVG floor plan (parity with the D1001 panel).
// Rooms with a polygon draw as walls + a live glance; monolithic rooms (attic/crawlspace) as chips.
// Clicking a room selects it (filters the Sensors list below). PWA renders real SVG, so this is cheap.
function HouseMap({ data, selected, onSelect }) {
  const unit = useTemp();
  if (!data || !Array.isArray(data.rooms) || data.rooms.length === 0) return null;
  const rooms = data.rooms;

  let mnx = 1e9, mny = 1e9, mxx = -1e9, mxy = -1e9;
  const grow = (x, y) => { mnx = Math.min(mnx, x); mny = Math.min(mny, y); mxx = Math.max(mxx, x); mxy = Math.max(mxy, y); };
  const scan = (ring) => ring.forEach(([x, y]) => grow(x, y));
  for (const r of rooms) {
    const g = r.geometry; if (!g) continue;
    if (g.poly) scan(g.poly);
    if (g.polys) g.polys.forEach(scan);
    if (g.label) grow(g.label[0], g.label[1]);
  }
  if (!(mxx > mnx && mxy > mny)) return null;
  const W = mxx - mnx, H = mxy - mny;

  // One device's curated glance = the FIRST TWO non-raw readings (GRAPHABLE order leads with
  // temp+hum), or an actuator's run state. Mirrors the D1001 panel's map (dev_compact): index data,
  // never raw (voc_raw etc.). temp respects the °F/°C pref; humidity shown as a bare "%".
  const fmtG = (g, v) => {
    if (isTempMetric(g.key)) return `${Math.round(convT(v, unit))}°`;
    if (g.key === "humidity_pct") return `${Math.round(v)}%`;
    const u = g.unit && g.unit[0] === "%" ? "%" : g.unit;
    return `${Math.round(v)}${u || ""}`;
  };
  const devGlance = (d) => {
    if (d.role === "actuator" && d.state)
      return `${d.state.running ? "▶" : "⏸"} ${d.state.status || (d.state.running ? "on" : "idle")}`;
    const m = d.metrics || {}, parts = [];
    for (const g of GRAPHABLE) {
      if (g.key.endsWith("_raw")) continue;                 // index metrics only — never raw
      const v = m[g.key];
      if (typeof v !== "number") continue;
      parts.push(fmtG(g, v));
      if (parts.length >= 2) break;
    }
    return parts.join(" · ");
  };
  const devColor = (d, act) => {
    if (d.out_of_range && Object.keys(d.out_of_range).length) return "#ef4444";   // out of range
    if (d.role === "actuator" && d.state) return d.state.running ? "#22c55e" : "#94a3b8";
    return act ? "#b4823c" : "#3d6e93";
  };
  const roomGlance = (devs) => { for (const d of devs || []) { const g = devGlance(d); if (g) return g; } return ""; };
  const aqBadge = (aq) => {                 // room.air_quality summary → {label,color,title} or null (AQ_BAND_COLOR is module-level)
    if (!aq || !aq.air_quality_band) return null;
    const rel = aq.air_quality_basis === "relative";   // "~" hints a relative measure (not comparable room-to-room)
    return { label: `● ${aq.air_quality_band_label}${rel ? " ~" : ""}`,
             color: AQ_BAND_COLOR[aq.air_quality_band] || "#94a3b8", title: aq.explanation || "" };
  };
  const hasPlace = (d) => d.placement && typeof d.placement.x === "number" && typeof d.placement.y === "number";
  // Placements are normalized to the POLYGON bbox (matching RoomPlacementEditor's toNorm) — not the
  // label-inclusive bbox — so a device lands exactly where it was dropped in the editor.
  const polyBox = (g) => {
    let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
    (g.polys || [g.poly]).forEach((r) => r.forEach(([x, y]) => {
      x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y);
    }));
    return { x0, y0, W: x1 - x0, H: y1 - y0 };
  };

  const hasPoly = (r) => r.geometry && (r.geometry.poly || r.geometry.polys);
  const placed = rooms.filter(hasPoly);
  const mono = rooms.filter((r) => !hasPoly(r) && (r.counts.sensors + r.counts.actuators) > 0);

  return html`
    <div class="housemap-wrap">
      <h2 class="section">House</h2>
      <svg class="housemap" viewBox="${mnx} ${mny} ${W} ${H}" preserveAspectRatio="xMidYMid meet"
           style="width:100%;max-height:74vh;display:block">
        ${placed.map((r) => {
          const g = r.geometry, act = r.counts.actuators > 0, sel = selected === r.id;
          const rings = g.polys || [g.poly];
          const [lx, ly] = g.label || [(mnx + mxx) / 2, (mny + mxy) / 2];
          const box = polyBox(g);
          // Keep the room name inside the room: if the label (est. at ~0.55em/char, size 26) would
          // overrun the polygon bbox, squeeze it with textLength so it stays within the geometry.
          const nameLines = wrapLabel(r.name, box.W * 0.92, 26);   // word-wrap + per-line squeeze
          const labelDrop = (nameLines.length - 1) * 25;           // px the wrapped lines add below the anchor
          const devs = r.devices || [];
          const placedDevs = devs.filter(hasPlace);
          const unplaced = devs.filter((d) => !hasPlace(d));
          const fill = sel ? (act ? "rgba(180,130,60,0.34)" : "rgba(47,126,122,0.30)")
                           : (act ? "rgba(180,130,60,0.14)" : "rgba(47,126,122,0.12)");
          const stroke = act ? "#b4823c" : "#2f7e7a";
          const unplacedGl = unplaced.map(devGlance).filter(Boolean).join("   ");
          const aq = aqBadge(r.air_quality);
          return html`<g class="room" style="cursor:pointer" onClick=${() => onSelect(r.id, r.name)}>
            ${rings.map((ring) => html`<polygon points=${ring.map((p) => p.join(",")).join(" ")}
              fill=${fill} stroke=${stroke} stroke-width=${sel ? 6 : 4} stroke-linejoin="round" />`)}
            <text x=${lx} y=${ly - 2} text-anchor="middle" font-size="26" font-weight="600"
              fill="currentColor">${nameLines.map((ln, i) => html`<tspan x=${lx} dy=${i === 0 ? 0 : 25}
                textLength=${ln.tl} lengthAdjust=${ln.tl ? "spacingAndGlyphs" : undefined}>${ln.text}</tspan>`)}</text>
            ${aq && html`<text x=${lx} y=${ly + 20 + labelDrop} text-anchor="middle" font-size="18" font-weight="700"
              fill=${aq.color} paint-order="stroke" stroke="rgba(9,12,22,0.72)" stroke-width="3.5">
              <title>${aq.title}</title>${aq.label}</text>`}
            ${placedDevs.map((d) => {
              const t = devGlance(d); if (!t) return null;
              const cx = box.x0 + d.placement.x * box.W, cy = box.y0 + d.placement.y * box.H;
              return html`<text x=${cx} y=${cy} text-anchor="middle" dominant-baseline="middle"
                font-size="19" font-weight="600" fill=${devColor(d, act)} style="pointer-events:none"
                paint-order="stroke" stroke="rgba(9,12,22,0.72)" stroke-width="3.5">${t}</text>`;
            })}
            ${unplacedGl && html`<text x=${lx} y=${ly + (aq ? 40 : 22) + labelDrop} text-anchor="middle" font-size="19"
              fill=${act ? "#b4823c" : "#3d6e93"} style="pointer-events:none">${unplacedGl}</text>`}
          </g>`;
        })}
      </svg>
      ${mono.length > 0 && html`<div class="mono-row" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">
        ${mono.map((r) => html`<button class=${"btn sm" + (selected === r.id ? "" : " ghost")}
          onClick=${() => onSelect(r.id, r.name)}>${r.name}${roomGlance(r.devices) ? ` · ${roomGlance(r.devices)}` : ""}</button>`)}
      </div>`}
      ${Array.isArray(data.air_quality_legend) && html`<div class="aq-legend"
        style="display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-top:10px;font-size:12px;color:#9fb0c3">
        <span style="opacity:.7">Air quality:</span>
        ${data.air_quality_legend.map((L) => html`<span title=${L.meaning} style="display:inline-flex;align-items:center;gap:4px">
          <span style=${`width:10px;height:10px;border-radius:2px;background:${AQ_BAND_COLOR[L.band] || "#94a3b8"};display:inline-block`}></span>${L.label}</span>`)}
        <span style="opacity:.55">· "~" = relative to the room's own baseline</span>
      </div>`}
    </div>`;
}

// live wall-clock for the top icon bar. Reflects the viewing device's local time; ticks each second.
function Clock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  const hh = now.getHours(), mm = String(now.getMinutes()).padStart(2, "0");
  const ampm = hh >= 12 ? "PM" : "AM", h12 = ((hh + 11) % 12) + 1;
  return html`<span class="clock" title=${now.toLocaleString()}>${h12}:${mm}<span class="clock-ampm">${ampm}</span></span>`;
}

function App() {
  const [devices, setDevices] = useState(null);
  const [sensors, setSensors] = useState(null);
  const [rooms, setRooms] = useState(null);         // /api/v1/rooms graph (house map)
  const [selRoom, setSelRoom] = useState(null);     // {id,name} filtering the Sensors list, or null
  const [alerts, setAlerts] = useState([]);
  const [weather, setWeather] = useState(null);
  const [status, setStatus] = useState("init");      // init | live | down
  const [isAdmin, setIsAdmin] = useState(!!getToken());
  const [showAdmin, setShowAdmin] = useState(false);
  const [tempUnit, setTempUnit] = useState(tempPref());
  const [editDevice, setEditDevice] = useState(null);   // R8 edit modal target
  const [showAdd, setShowAdd] = useState(false);        // add-device modal
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
      // house map is additive — fetch out-of-band so an older server (no /api/v1/rooms) can't break the dashboard
      getJSON("/api/v1/rooms").then(setRooms).catch(() => setRooms(null));
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
        <div class="topbar-row">
          <div class="dot ${status === "live" ? "live" : status === "down" ? "down" : ""}" title=${"build " + BUILD}></div>
          <div class="spacer"></div>
          <${Clock} />
          <${SceneSelector} isAdmin=${isAdmin} onNeedAdmin=${() => setShowAdmin(true)} onChange=${refresh} />
          <${NightMode} isAdmin=${isAdmin} onNeedAdmin=${() => setShowAdmin(true)} />
        </div>
        <div class="topbar-row topbar-row-2">
          <button class="btn sm ghost" onClick=${toggleUnit} title="temperature unit">°${tempUnit}</button>
          <${NotifyToggle} />
          ${isAdmin
            ? html`<span class="admin-on" title="Admin unlocked">🔓 Admin</span>
                   <button class="btn sm ghost" onClick=${() => setShowAdd(true)}>+ Device</button>
                   <button class="btn sm ghost" onClick=${lock}>Lock</button>`
            : html`<button class="btn sm" onClick=${() => setShowAdmin(true)}>🔒 Admin</button>`}
        </div>
      </div>

      <${AlertsBanner} alerts=${alerts} />

      <${HouseMap} data=${rooms} selected=${selRoom && selRoom.id}
        onSelect=${(id, name) => setSelRoom((p) => (p && p.id === id ? null : { id, name }))} />

      ${selRoom && rooms && (() => {
        const rm = (rooms.rooms || []).find((r) => r.id === selRoom.id);
        return rm ? html`<${RoomPlacementEditor} room=${rm} isAdmin=${isAdmin}
          onNeedAdmin=${() => setShowAdmin(true)} onSaved=${refresh} />` : null;
      })()}

      ${devices == null && html`<div class="empty">Loading…</div>`}
      <${Actuators} devices=${devices} sensors=${sensors} isAdmin=${isAdmin}
        onChange=${refresh} onNeedAdmin=${() => setShowAdmin(true)} onEdit=${onEdit} />

      <${Sensors} sensors=${sensors} isAdmin=${isAdmin} onEdit=${onEdit} onChange=${refresh}
        roomFilter=${selRoom} onClearRoom=${() => setSelRoom(null)} />
      <${GraphBuilder} sensors=${sensors} weather=${weather} />

      ${status === "down" && html`<p class="note">⚠ Can't reach the server — showing last known state.</p>`}

      ${showAdmin && html`<${AdminModal} onClose=${() => setShowAdmin(false)}
        onUnlock=${() => setIsAdmin(true)} />`}
      ${editDevice && html`<${DeviceMetaModal} device=${editDevice}
        areas=${(rooms && Array.isArray(rooms.rooms) ? rooms.rooms.map((r) => r.id).sort() : [])}
        onClose=${() => setEditDevice(null)} onSaved=${refresh} />`}
      ${showAdd && html`<${AddDeviceModal} onClose=${() => setShowAdd(false)} onSaved=${refresh} />`}
    </div>
    </${UnitsCtx.Provider}>`;
}

render(html`<${App} />`, document.getElementById("root"));

// register the service worker (offline app-shell). Best-effort; ignore on http/file contexts.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/app/sw.js").catch(() => {});
}
