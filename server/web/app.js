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
const BUILD = "v33 LED indicator: manual toggle (night mode WIP)";

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
function ManualControl({ vm, isAdmin, onChange, onNeedAdmin }) {
  const traits = vm.traits || {};
  const act = vm.actuator || {};
  const sp = traits.setpoint, rg = traits.ranged, ind = traits.indicator;
  const [open, setOpen] = useState(false);
  const [target, setTarget] = useState(act.target_pct ?? (sp && sp.safe_value) ?? "");
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");

  if (!sp && !rg && !ind) return null;                 // device exposes no manual functions

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
      ${ind && html`
        <div class="field"><label>LED / panel light</label>
          <div class="controls">
            <button class="btn sm" disabled=${busy === "led"}
              onClick=${() => cmd("indicator", { on: true }, "led")}>On</button>
            <button class="btn sm" disabled=${busy === "led"}
              onClick=${() => cmd("indicator", { on: false }, "led")}>Off</button>
          </div>
          ${act.led_on != null && html`<span class="note">now: ${act.led_on ? "on" : "off"}</span>`}
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

function DeviceCard({ vm, sensors, isAdmin, onChange, onNeedAdmin, onEdit }) {
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
        <h2>${dispName(vm)}</h2>
        ${vm.room && html`<span class="sensor-area">${vm.room}</span>`}
        <span class="badge health-${vm.health}">${vm.health}</span>
        <button class="btn sm ghost edit-btn" onClick=${() => onEdit(vm)}>✎</button>
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
      <${OverrideControls} id=${vm.device_id} override=${vm.override} isAdmin=${isAdmin}
        onChange=${onChange} onNeedAdmin=${onNeedAdmin} />
      <${ManualControl} vm=${vm} isAdmin=${isAdmin} onChange=${onChange} onNeedAdmin=${onNeedAdmin} />
      ${ranged
        ? html`<${RangedSettings} vm=${vm} isAdmin=${isAdmin} onChange=${onChange} onNeedAdmin=${onNeedAdmin} />`
        : html`<${SettingsPanel} vm=${vm} sensors=${sensors} isAdmin=${isAdmin}
            onChange=${onChange} onNeedAdmin=${onNeedAdmin} />`}
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
  { key: "dewpoint_c", unit: "°C", color: "#22d3ee", label: "Dew point" },
  { key: "co2_ppm", unit: "ppm", color: "#fbbf24", label: "CO₂" },
  { key: "radon_bqm3", unit: "Bq", color: "#a78bfa", label: "Radon" },
  { key: "pressure_hpa", unit: "hPa", color: "#34d399", label: "Pressure" },
  { key: "pm25_ugm3", unit: "µg/m³", color: "#fb7185", label: "PM2.5" },
  { key: "aqi", unit: "", color: "#fbbf24", label: "AQI" },
];

// shared value row (temp respects the °F/°C pref)
function SensorVals({ m, unit }) {
  return html`<div class="sensor-vals">
    ${m.temperature_c != null && html`<span class="sv"><b>${round1(convT(m.temperature_c, unit))}°</b>${unit}</span>`}
    ${m.humidity_pct != null && html`<span class="sv"><b>${round1(m.humidity_pct)}</b>%RH</span>`}
    ${m.dewpoint_c != null && html`<span class="sv"><b>${round1(convT(m.dewpoint_c, unit))}°</b>${unit} Dew</span>`}
    ${m.co2_ppm != null && html`<span class="sv"><b>${Math.round(m.co2_ppm)}</b>ppm</span>`}
    ${m.radon_bqm3 != null && html`<span class="sv"><b>${Math.round(m.radon_bqm3)}</b>Bq</span>`}
    ${m.pressure_hpa != null && html`<span class="sv"><b>${Math.round(m.pressure_hpa)}</b>hPa</span>`}
    ${m.pm25_ugm3 != null && html`<span class="sv"><b>${Math.round(m.pm25_ugm3)}</b>µg/m³</span>`}
    ${m.aqi != null && html`<span class="sv"><b>${m.aqi}</b>AQI</span>`}
    ${m.fan_speed != null && html`<span class="sv"><b>${m.fan_on === 0 ? "off" : m.fan_speed}</b> fan</span>`}
    ${m.filter_life_pct != null && html`<span class="sv"><b>${Math.round(m.filter_life_pct)}</b>% filter${m.filter_low ? " ⚠️" : ""}</span>`}
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
  const [managed, setManaged] = useState(null);               // null=not loaded; {hidden:[], retired:[]}
  if (sensors == null) return null;
  if (sensors.length === 0) return html`<p class="note">No sensor readings yet.</p>`;
  const open = (id) => setExpanded((p) => new Set(p).add(id));
  const close = (id) => setExpanded((p) => { const n = new Set(p); n.delete(id); return n; });
  const mins = sensors.filter((s) => !expanded.has(s.device_id));
  const exps = sensors.filter((s) => expanded.has(s.device_id));

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
function DeviceMetaModal({ device, onClose, onSaved }) {
  const [name, setName] = useState(device.name || "");
  const [room, setRoom] = useState(device.room || (device.area ? prettyArea(device.area) : ""));
  const [hidden, setHidden] = useState(!!device.hidden);
  const [retired, setRetired] = useState(!!device.retired);
  const [offsets, setOffsets] = useState({ ...(device.offsets || {}) });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const metrics = GRAPHABLE.filter((g) => device.metrics && device.metrics[g.key] != null);
  const id = encodeURIComponent(device.device_id);
  const save = async () => {
    setBusy(true); setErr("");
    try {
      await adminSend("PUT", `/api/v1/devices/${id}/meta`, { name, room, hidden, retired });
      for (const g of metrics) {               // PUT only the offsets that changed
        const v = Number(offsets[g.key] || 0);
        if (v !== Number((device.offsets || {})[g.key] || 0)) {
          await adminSend("PUT", `/api/v1/devices/${id}/calibration`, { metric: g.key, offset: v });
        }
      }
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
          onChange=${(e) => setHidden(e.target.checked)} /> Hide from dashboard (temporary)</label>
        <label class="switch"><input type="checkbox" checked=${retired}
          onChange=${(e) => setRetired(e.target.checked)} /> Retire (decommissioned — archives it)</label>
        ${retired && !device.retired && html`<p class="note">Retiring archives the device — history is kept,
          it's removed from the dashboard, and it's not expected to report again. Restore it later from the
          "retired" list.</p>`}
        ${metrics.length > 0 && html`
          <div class="divider"></div>
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
      </div>
    </div>`;
}

const KNOWN_TRAITS = ["switchable", "ranged", "setpoint", "lockable", "positionable"];

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
  const toggleTrait = (t) => setTraits((ts) => (ts.includes(t) ? ts.filter((x) => x !== t) : [...ts, t]));
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
  const sensorFields = html`
    <input value=${mac} placeholder="MAC — e.g. AA:BB:CC:DD:EE:FF"
      onInput=${(e) => setMac(e.target.value.toUpperCase())} />
    <input value=${deviceType} placeholder="device_type — e.g. switchbot_meter_pro, aranet_radon_plus"
      onInput=${(e) => setDeviceType(e.target.value)} />
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
    <input value=${deviceId} placeholder=${`device_id slug — e.g. ${kind === "sensor" ? "meter_living_room" : "lamp_office"}`}
      onInput=${(e) => setDeviceId(e.target.value)} />
    <input value=${area} placeholder="area slug — e.g. living_room" onInput=${(e) => setArea(e.target.value)} />
    ${kind === "sensor" ? sensorFields : actuatorFields}
    ${err && html`<div class="err">${err}</div>`}
    <div class="modal-actions">
      <button class="btn ghost" onClick=${onClose}>Cancel</button>
      <button class="btn primary" disabled=${busy} onClick=${save}>${busy ? "Adding…" : "Add"}</button>
    </div>`;
  const success = html`
    <p class="note">✅ Registered <b>${done && done.device_id}</b>.</p>
    <p class="note">${done && done.note}</p>
    <p class="note"><code>${done && done.reload_cmd}</code></p>
    <div class="modal-actions">
      <button class="btn primary" onClick=${() => { onSaved(); onClose(); }}>Done</button>
    </div>`;
  return html`
    <div class="modal-bg" onClick=${onClose}>
      <div class="modal" onClick=${(e) => e.stopPropagation()}>
        <h3>Add a ${kind}</h3>
        <p class="note">${kind === "actuator"
          ? "Appends to control.yaml. The node must already be enrolled, or it won't be commandable."
          : "Appends to the sensor registry (devices.yaml)."}</p>
        ${done ? success : form}
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
        <${SceneSelector} isAdmin=${isAdmin} onNeedAdmin=${() => setShowAdmin(true)} onChange=${refresh} />
        <button class="btn sm ghost" onClick=${toggleUnit} title="temperature unit">°${tempUnit}</button>
        <${NotifyToggle} />
        ${isAdmin
          ? html`<span class="admin-on" title="Admin unlocked">🔓 Admin</span>
                 <button class="btn sm ghost" onClick=${() => setShowAdd(true)}>+ Device</button>
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
      ${showAdd && html`<${AddDeviceModal} onClose=${() => setShowAdd(false)} onSaved=${refresh} />`}
    </div>
    </${UnitsCtx.Provider}>`;
}

render(html`<${App} />`, document.getElementById("root"));

// register the service worker (offline app-shell). Best-effort; ignore on http/file contexts.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/app/sw.js").catch(() => {});
}
