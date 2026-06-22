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
function SettingsPanel({ vm, isAdmin, onChange, onNeedAdmin }) {
  const c = vm.control || {};
  const [open, setOpen] = useState(false);
  const [enabled, setEnabled] = useState(!!c.enabled);
  const [onAbove, setOnAbove] = useState(c.on_above ?? "");
  const [offBelow, setOffBelow] = useState(c.off_below ?? "");
  const [quiet, setQuiet] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [flash, setFlash] = useState("");

  const save = async () => {
    if (!isAdmin) return onNeedAdmin();
    setBusy(true); setErr(""); setFlash("");
    const patch = {
      enabled,
      control: { strategy: c.strategy || "hysteresis", on_above: Number(onAbove), off_below: Number(offBelow) },
    };
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
      <p class="note">Source sensor: ${vm.control.source_sensor || "—"} · strategy: ${c.strategy}</p>
    </div>`;
}

// ── device card ──────────────────────────────────────────────────────────────
function DeviceCard({ vm, isAdmin, onChange, onNeedAdmin }) {
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
      <${SettingsPanel} vm=${vm} isAdmin=${isAdmin} onChange=${onChange} onNeedAdmin=${onNeedAdmin} />
    </div>`;
}

// ── admin unlock modal ───────────────────────────────────────────────────────
function AdminModal({ onClose, onUnlock }) {
  const [pw, setPw] = useState("");
  const [busy, setBusy] = useState(false);
  const inputRef = useRef(null);
  useEffect(() => { inputRef.current && inputRef.current.focus(); }, []);
  const submit = async () => {
    if (!pw) return;
    setBusy(true);
    const tok = await deriveToken(pw);
    setToken(tok); onUnlock(); onClose();
  };
  return html`
    <div class="modal-bg" onClick=${onClose}>
      <div class="modal" onClick=${(e) => e.stopPropagation()}>
        <h3>Admin unlock</h3>
        <p class="note">Enter the master passphrase. Only the derived token is stored locally.</p>
        <input ref=${inputRef} type="password" value=${pw} placeholder="master passphrase"
          onInput=${(e) => setPw(e.target.value)}
          onKeyDown=${(e) => e.key === "Enter" && submit()} />
        <div class="modal-actions">
          <button class="btn ghost" onClick=${onClose}>Cancel</button>
          <button class="btn primary" disabled=${busy || !pw} onClick=${submit}>Unlock</button>
        </div>
      </div>
    </div>`;
}

// ── app shell ────────────────────────────────────────────────────────────────
function App() {
  const [devices, setDevices] = useState(null);
  const [status, setStatus] = useState("init");      // init | live | down
  const [isAdmin, setIsAdmin] = useState(!!getToken());
  const [showAdmin, setShowAdmin] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const data = await getJSON("/api/v1/displays");
      setDevices(data.devices || []);
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
          ? html`<button class="btn sm ghost" onClick=${lock}>🔓 Lock</button>`
          : html`<button class="btn sm" onClick=${() => setShowAdmin(true)}>🔒 Admin</button>`}
      </div>

      ${devices == null && html`<div class="empty">Loading…</div>`}
      ${devices != null && devices.length === 0 && html`<div class="empty">No controllable devices.</div>`}
      ${devices && devices.map((vm) => html`
        <${DeviceCard} key=${vm.device_id} vm=${vm} isAdmin=${isAdmin}
          onChange=${refresh} onNeedAdmin=${() => setShowAdmin(true)} />`)}

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
