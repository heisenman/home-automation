"""
Home automation query API — FastAPI / DuckDB backend.

Endpoints:
  GET /devices                       — all devices with last reading
  GET /devices/{device_id}/summary   — daily summary rows (default 7 days)
  GET /devices/{device_id}/readings  — bounded raw readings via DuckDB
  GET /health                        — liveness + last compaction time

Deep-dive queries are bounded (MAX_DEEP_ROWS) and serialized through a single
asyncio lock to prevent query-DoS against the single server (§9).

Usage:
  uvicorn server.api.main:app --host 0.0.0.0 --port 8123 --workers 1
  or:
  python3 -m server.api.main
"""

import asyncio
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import duckdb
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ── Config ────────────────────────────────────────────────────────────────────

DB_PATH = Path(os.environ.get("HA_DB", "instance/db/hot.db"))
PARQUET_GLOB = Path(os.environ.get("HA_PARQUET_DIR", "instance/db/parquet"))
WEATHER_DB = Path(os.environ.get("HA_WEATHER_DB", "instance/db/weather.db"))
WEATHER_TABLE = os.environ.get("HA_WEATHER_TABLE", "weather")
MAX_DEEP_ROWS: int = int(os.environ.get("HA_MAX_DEEP_ROWS", "50000"))

log = logging.getLogger("ha.api")

_deep_query_lock = asyncio.Lock()


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("API starting — db=%s parquet=%s", DB_PATH, PARQUET_GLOB)
    yield
    log.info("API shutdown")


app = FastAPI(title="Home Automation API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],   # POST for the control router (/devices/{id}/command)
    allow_headers=["*"],
)


# ── Control plane (mounted only when configured) ─────────────────────────────────
# The read/dashboard endpoints above stay open on the LAN (read-only). The control router is mounted
# ONLY when the master passphrase is present (→ admin bearer + confirm token derivable) AND degrades
# gracefully otherwise, so a box without control config still serves reads. Admin auth = SHA-derived
# bearer; sensitive actions need the separate confirm token (ADR-0010/0011, go-live 2026-06-21).
CONTROL_REGISTRY = Path(os.environ.get("HA_CONTROL_REGISTRY", "instance/control.yaml"))
NODE_SECRETS_LUT = Path(os.environ.get("HA_NODE_SECRETS", "instance/node_secrets.enc"))
CONTROL_POLICY = Path(os.environ.get("HA_CONTROL_POLICY", "instance/control_policy.yaml"))
CONTROL_SECRETS = Path(os.environ.get("HA_CONTROL_SECRETS", "instance/control_secrets.yaml"))
MIDEA_DEVICE_ENV = Path(os.environ.get("HA_MIDEA_DEVICE_ENV", "instance/midea-device.env"))
CONTROL_DB = Path(os.environ.get("HA_CONTROL_DB", "instance/db/control.db"))
WEB_DIR = Path(__file__).resolve().parents[1] / "web"   # server/web — the no-build PWA


def _mount_control(app: FastAPI) -> None:
    # EVERYTHING (incl. the control-package imports, which pull in cryptography) is guarded: a missing
    # optional dep or any config error must DISABLE control, never crash the read API at import time.
    try:
        from server.control.secret_store import (available_master, load_lut, make_api_token_verifier,
                                                 make_confirm_verifier)
        master = available_master()
        if not master:
            log.info("control plane NOT mounted — no master passphrase (set HA_MASTER_PASSPHRASE or "
                     "instance/.master_pass to enable /devices command API)")
            return
        import yaml
        from server.control.bootstrap import build_issuer
        from server.control.registry import check_secrets_present
        from server.api.control import make_device_meta_router, make_override_router, make_router

        broker = os.environ.get("HA_BROKER", "localhost")
        port = int(os.environ.get("HA_BROKER_PORT", "1883"))
        # shared construction (server/control/bootstrap) — one source of truth for the issuer wiring,
        # used identically by the controller, so the security plumbing can't drift between them.
        issuer, registry, _drivers = build_issuer(
            master, control_registry=CONTROL_REGISTRY, node_secrets_lut=NODE_SECRETS_LUT,
            control_policy=CONTROL_POLICY, control_secrets=CONTROL_SECRETS,
            midea_device_env=MIDEA_DEVICE_ENV, broker=broker, port=port)
        api_authz = make_api_token_verifier(master)
        app.include_router(make_router(issuer, make_confirm_verifier(master), api_authz))
        # the manual-override + control-state router (writes control.db, read by the controller each tick)
        app.include_router(make_override_router(api_authz, CONTROL_DB, device_ids=set(registry)))
        app.include_router(make_device_meta_router(api_authz, CONTROL_DB))   # R8 friendly-name/room/hide
        app.state.control_registry = registry      # device_id -> DeviceCtl (traits for manual-control UI)
        missing = check_secrets_present(registry, issuer.secrets)
        log.info("control plane MOUNTED — %d device(s), %d controllable; broker %s:%s%s",
                 len(registry), len(issuer.secrets), broker, port,
                 f"; NO secret for {missing}" if missing else "")
    except Exception:
        log.exception("control plane mount FAILED — read API stays up, control disabled")


_mount_control(app)

# ── Web app (PWA) — self-contained, no build step (server/web). Mounted at /app; "/" serves it. ──
if WEB_DIR.exists():
    app.mount("/app", StaticFiles(directory=str(WEB_DIR), html=True), name="webapp")


@app.middleware("http")
async def _no_cache_shell(request, call_next):
    # The app shell must always revalidate so a code push is never masked by a stale HTTP cache. The SW is
    # network-first too; together a deploy is visible on the next load. API/data responses are untouched.
    resp = await call_next(request)
    p = request.url.path
    if p == "/" or p.startswith("/app"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


# ── DB helpers ────────────────────────────────────────────────────────────────

def _hot_conn() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise HTTPException(status_code=503, detail="Hot database not yet initialised")
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _parquet_glob() -> str:
    return str(PARQUET_GLOB / "year=*" / "month=*" / "*.parquet")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def root():
    # The PWA is the front door. Fall back to the frozen inline dashboard if the web bundle is absent.
    index = WEB_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return HTMLResponse(_DASHBOARD_HTML)


@app.get("/legacy", response_class=HTMLResponse, include_in_schema=False)
def legacy_dashboard():
    # Frozen 2026-06-22 when the PWA (server/web) became the primary UI. Kept as a zero-dependency
    # fallback / sanity page; no longer developed. See ADR-0013.
    return _DASHBOARD_HTML


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Home Sensors</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;background:#0f1117;color:#e2e8f0;padding:1.25rem}
header{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:1.25rem}
h1{font-size:1.1rem;font-weight:600;color:#94a3b8;letter-spacing:.03em}
#ts{font-size:.72rem;color:#475569}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:.875rem}
.card{background:#1a1f2e;border-radius:12px;padding:1rem;border:1px solid #252d3d;transition:border-color .2s}
.card:hover{border-color:#3b4a6b}
.card.stale{border-color:#3d2b1f;opacity:.65}
.area{font-size:.7rem;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.07em;margin-bottom:.2rem}
.did{font-size:.65rem;color:#374151;margin-bottom:.7rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.temp{font-size:2.4rem;font-weight:700;line-height:1;color:#f1f5f9;margin-bottom:.5rem}
.temp sup{font-size:1rem;color:#64748b;font-weight:400}
.metrics{display:flex;flex-wrap:wrap;gap:.6rem .9rem;margin-bottom:.7rem}
.m{font-size:.85rem}
.ml{font-size:.6rem;color:#4b5563;margin-bottom:.1rem}
.bat-ok{color:#4ade80}.bat-warn{color:#facc15}.bat-low{color:#f87171}.bat-raw{color:#6b7280}
.foot{display:flex;justify-content:space-between;font-size:.65rem;color:#374151;padding-top:.6rem;border-top:1px solid #1e2433}
.stale-pill{background:#3d1f1f;color:#f87171;border-radius:4px;padding:.1rem .35rem;font-size:.6rem}
.no-data{color:#374151;font-size:2rem;margin-bottom:.5rem}
.card{cursor:pointer}
/* detail view */
#detail{display:none}
#detail.show{display:block}
#grid.hide{display:none}
.back{background:#1a1f2e;border:1px solid #252d3d;color:#94a3b8;border-radius:8px;padding:.4rem .8rem;font-size:.8rem;cursor:pointer}
.back:hover{border-color:#3b4a6b}
.dtitle{font-size:1.4rem;font-weight:700;color:#f1f5f9;margin:.2rem 0}
.dsub{font-size:.72rem;color:#475569;margin-bottom:1rem}
.ranges{display:flex;gap:.4rem;margin-bottom:1rem}
.rbtn{background:#1a1f2e;border:1px solid #252d3d;color:#94a3b8;border-radius:8px;padding:.35rem .8rem;font-size:.78rem;cursor:pointer}
.rbtn.active{background:#1e3a5f;border-color:#3b82f6;color:#dbeafe}
.chart-box{background:#1a1f2e;border:1px solid #252d3d;border-radius:12px;padding:1rem 1rem .5rem;margin-bottom:1rem}
.chart-title{font-size:.78rem;font-weight:600;color:#94a3b8;margin-bottom:.5rem;display:flex;justify-content:space-between}
.chart-cur{color:#e2e8f0}
svg .axis{stroke:#2a3344;stroke-width:1}
svg .grid-l{stroke:#1e2433;stroke-width:1}
svg .lbl{fill:#475569;font-size:10px}
svg .band{opacity:.18}
.loading{color:#475569;padding:1.5rem;font-size:.85rem}
/* graphs workspace */
#graphs{display:none}
#graphs.show{display:block}
header .right{display:flex;align-items:center;gap:.6rem}
.navbtn{background:#1a1f2e;border:1px solid #252d3d;color:#94a3b8;border-radius:8px;padding:.35rem .8rem;font-size:.78rem;cursor:pointer}
.navbtn:hover{border-color:#3b4a6b}
.gcard{background:#141925;border:1px solid #252d3d;border-radius:12px;padding:1rem;margin-bottom:1.25rem}
.ghead{display:flex;justify-content:space-between;align-items:center;gap:.5rem;margin-bottom:.6rem}
.gtitle{font-size:1rem;font-weight:600;color:#e2e8f0;background:transparent;border:none;outline:none;border-bottom:1px dashed #2a3344}
.gtitle:focus{border-bottom-color:#3b82f6}
.legend{display:flex;flex-wrap:wrap;gap:.5rem;margin:.5rem 0}
.chip{display:flex;align-items:center;gap:.35rem;background:#1a1f2e;border:1px solid #252d3d;border-radius:20px;padding:.2rem .6rem;font-size:.72rem;color:#cbd5e1}
.chip .sw{width:10px;height:10px;border-radius:50%}
.chip .x{cursor:pointer;color:#64748b}
.chip .x:hover{color:#f87171}
.addrow{display:flex;flex-wrap:wrap;gap:.4rem;align-items:center;margin:.5rem 0}
.sel{background:#1a1f2e;border:1px solid #252d3d;color:#e2e8f0;border-radius:8px;padding:.3rem .45rem;font-size:.75rem}
.del{color:#64748b;cursor:pointer;font-size:.72rem}
.del:hover{color:#f87171}
</style>
</head>
<body>
<header>
  <h1 id="hdr">Home Sensors</h1>
  <div class="right"><span id="ts"></span><button class="navbtn" id="navbtn" onclick="toggleView()">📊 Graphs</button></div>
</header>
<div class="grid" id="grid"><div style="color:#475569;padding:2rem">Loading…</div></div>
<div id="detail"></div>
<div id="graphs"></div>
<script>
const REFRESH=30000;
const toF=c=>(c*9/5+32).toFixed(1);
const fToScale=c=>c*9/5+32;
let DEVICES=[], CURRENT=null, RANGE=86400;  // RANGE = span in seconds (default 24h)
const RAW_MAX_S=7*86400;  // span <= this -> full raw points; beyond -> daily min/avg/max

function ago(ts){
  if(!ts)return'—';
  const s=(Date.now()-new Date(ts).getTime())/1000;
  if(s<60)return Math.round(s)+'s ago';
  if(s<3600)return Math.round(s/60)+'m ago';
  return Math.round(s/3600)+'h ago';
}
function batClass(v){
  if(v===null||v===undefined)return'';
  if(v>100)return'bat-raw';
  if(v>50)return'bat-ok';
  if(v>20)return'bat-warn';
  return'bat-low';
}
function getM(rs,name){const r=rs&&rs.find(r=>r.metric===name);return r?r.value:null}
function lastTs(rs){if(!rs||!rs.length)return null;return rs.reduce((a,b)=>a.ts>b.ts?a:b).ts}
function titleCase(s){return s.replace(/_/g,' ').replace(/\\b\\w/g,c=>c.toUpperCase())}

function card(dev,last){
  const rs=last?last.readings:[];
  const tc=getM(rs,'temperature_c');
  const hum=getM(rs,'humidity_pct');
  const bat=getM(rs,'battery_pct');
  const rad=getM(rs,'radon_bqm3');
  const pres=getM(rs,'pressure_hpa');
  const ts=lastTs(rs)||dev.last_ts;
  const ageS=ts?(Date.now()-new Date(ts).getTime())/1000:Infinity;
  const stale=ageS>300;
  const area=titleCase(dev.area);
  const batLabel=bat!==null&&bat!==undefined?(bat>100?bat+'%*':bat+'%'):'';

  return`<div class="card${stale?' stale':''}" onclick="openDetail('${dev.device_id}')">
    <div class="area">${area}</div>
    <div class="did">${dev.device_id}</div>
    ${tc!==null
      ?`<div class="temp">${toF(tc)}<sup>°F</sup></div>`
      :`<div class="no-data">—</div>`}
    <div class="metrics">
      ${rad!==null?`<div class="m"><div class="ml">RADON</div>${Math.round(rad)}<span style="font-size:.55em"> Bq/m³</span></div>`:''}
      ${hum!==null?`<div class="m"><div class="ml">RH</div>${Math.round(hum)}%</div>`:''}
      ${pres!==null?`<div class="m"><div class="ml">PRES</div>${Math.round(pres)}<span style="font-size:.55em"> hPa</span></div>`:''}
      ${bat!==null?`<div class="m"><div class="ml">BAT</div><span class="${batClass(bat)}">${batLabel}</span></div>`:''}
      ${dev.last_rssi?`<div class="m"><div class="ml">RSSI</div>${dev.last_rssi}</div>`:''}
    </div>
    <div class="foot">
      <span>${ago(ts)}</span>
      ${stale?'<span class="stale-pill">stale</span>':''}
    </div>
  </div>`;
}

async function refresh(){
  if(CURRENT)return;  // pause grid refresh while in detail view
  if(document.getElementById('graphs').classList.contains('show'))return;  // …or graphs view
  try{
    // hide unregistered/stray devices (e.g. a neighbour's SwitchBot, or a rotating-MAC unit) — they
    // publish to the /raw discovery topic, not the grid. Registered devices have a real area/device_id.
    const devs=(await(await fetch('/devices')).json())
                 .filter(d=>d.area!=='unknown'&&!String(d.device_id).startsWith('unknown_'));
    DEVICES=devs;
    devs.sort((a,b)=>a.area.localeCompare(b.area));
    const lasts=await Promise.all(devs.map(async d=>{
      try{const r=await fetch('/devices/'+d.device_id+'/last');return r.ok?r.json():null}
      catch{return null}
    }));
    document.getElementById('grid').innerHTML=devs.map((d,i)=>card(d,lasts[i])).join('');
    document.getElementById('ts').textContent='updated '+new Date().toLocaleTimeString();
  }catch(e){console.error(e)}
}

// ── Deep-dive detail view ─────────────────────────────────────────────────
function openDetail(id){
  CURRENT=id; RANGE=86400;
  document.getElementById('grid').classList.add('hide');
  document.getElementById('detail').classList.add('show');
  document.getElementById('ts').textContent='';
  renderDetail();
}
function closeDetail(){
  CURRENT=null;
  document.getElementById('detail').classList.remove('show');
  document.getElementById('detail').innerHTML='';
  document.getElementById('grid').classList.remove('hide');
  document.getElementById('hdr').textContent='Home Sensors';
  refresh();
}
function setRange(s){RANGE=s; renderDetail();}
function applyCustomRange(){
  const n=parseFloat(document.getElementById('cr-n').value);
  const u=document.getElementById('cr-u').value;
  if(!(n>0)) return;
  RANGE=Math.round(n*({min:60,hour:3600,day:86400}[u]));
  renderDetail();
}
function fmtSpan(s){
  if(s<3600) return (s%60?(s/60).toFixed(1):s/60)+'m';
  if(s<86400) return (s%3600?(s/3600).toFixed(1):s/3600)+'h';
  return (s%86400?(s/86400).toFixed(1):s/86400)+'d';
}

function renderDetail(){
  const dev=DEVICES.find(d=>d.device_id===CURRENT)||{device_id:CURRENT,area:''};
  document.getElementById('hdr').textContent=titleCase(dev.area||CURRENT);
  const presets=[[3600,'1h'],[21600,'6h'],[86400,'24h'],[259200,'3d'],[604800,'7d'],[2592000,'30d'],[7776000,'90d']];
  const inp="background:#1a1f2e;border:1px solid #252d3d;color:#e2e8f0;border-radius:8px;padding:.35rem .5rem;font-size:.78rem";
  document.getElementById('detail').innerHTML=`
    <button class="back" onclick="closeDetail()">← all sensors</button>
    <div class="dtitle">${titleCase(dev.area||CURRENT)}</div>
    <div class="dsub">${CURRENT}</div>
    <div class="ranges">${presets.map(([s,l])=>
      `<button class="rbtn${RANGE===s?' active':''}" onclick="setRange(${s})">${l}</button>`).join('')}</div>
    <div class="ranges" style="margin-top:-.4rem">
      <span style="font-size:.75rem;color:#64748b;align-self:center">custom:</span>
      <input id="cr-n" type="number" min="1" step="1" placeholder="#" style="width:5rem;${inp}"
        onkeydown="if(event.key==='Enter')applyCustomRange()">
      <select id="cr-u" style="${inp}">
        <option value="min">minutes</option><option value="hour">hours</option><option value="day" selected>days</option>
      </select>
      <button class="rbtn" onclick="applyCustomRange()">Apply</button>
      <span style="font-size:.72rem;color:#475569;align-self:center">showing ${fmtSpan(RANGE)}</span>
    </div>
    <div id="charts"><div class="loading">Loading…</div></div>`;
  loadCharts();
}

// metric → (unit, color); convert temps to the °F scale, leave others raw. Charts render in this order
// for whichever metrics the device actually has (so an Aranet shows radon/pressure, not just temp/hum).
const MUNIT={temperature_c:'°F',humidity_pct:'%',pressure_hpa:'hPa',radon_bqm3:'Bq/m³',co2_ppm:'ppm',dew_point_c:'°F'};
const MCOL={temperature_c:'#fb923c',humidity_pct:'#38bdf8',pressure_hpa:'#a78bfa',radon_bqm3:'#4ade80',co2_ppm:'#facc15',dew_point_c:'#f472b6'};
const MORDER=['radon_bqm3','temperature_c','humidity_pct','pressure_hpa','co2_ppm','dew_point_c'];
const mConv=(m,v)=>(m==='temperature_c'||m==='dew_point_c')?fToScale(v):v;
const mLabel=m=>titleCase(MLABEL[m]||m.replace(/_/g,' '));

async function loadCharts(){
  const id=CURRENT, span=RANGE;
  const box=document.getElementById('charts');
  const end=new Date(), start=new Date(Date.now()-span*1000);
  try{
    if(span<=RAW_MAX_S){
      // Full-resolution raw points (hot SQLite + Parquet) — all of the device's metrics
      const url=`/devices/${id}/readings?start=${start.toISOString().slice(0,19)}Z`
               +`&end=${end.toISOString().slice(0,19)}Z&limit=50000`;
      const data=await(await fetch(url)).json();
      if(CURRENT!==id)return;
      const rs=data.readings||[];
      const present=new Set(rs.map(r=>r.metric).filter(m=>MUNIT[m]));
      const mets=MORDER.filter(m=>present.has(m));
      box.innerHTML='';
      for(const m of mets){
        const pts=rs.filter(r=>r.metric===m).map(r=>({t:+new Date(r.ts),v:mConv(m,r.value)}));
        const note=`${pts.length} raw pts · ${fmtSpan(span)}`+(data.truncated?' · capped 50k':'');
        box.appendChild(chartCard(`${mLabel(m)} (${MUNIT[m]}) — ${note}`,[{pts,color:MCOL[m]||'#fb923c',dots:true}],MUNIT[m]));
      }
      if(!mets.length)box.innerHTML='<div class="loading">No data in this range.</div>';
    }else{
      // Daily summary (min/avg/max band) for long spans — all of the device's metrics
      const days=Math.ceil(span/86400);
      const data=await(await fetch(`/devices/${id}/summary?days=${days}`)).json();
      if(CURRENT!==id)return;
      const sm=(data.summary||[]).filter(s=>!s.live);
      const present=new Set(sm.map(s=>s.metric).filter(m=>MUNIT[m]));
      const mets=MORDER.filter(m=>present.has(m));
      box.innerHTML='';
      for(const m of mets){
        const pts=sm.filter(s=>s.metric===m).map(s=>({t:+new Date(s.date),lo:mConv(m,s.min_val),hi:mConv(m,s.max_val),v:mConv(m,s.mean_val)}));
        box.appendChild(chartCard(`${mLabel(m)} (${MUNIT[m]}) — daily min/avg/max · ${days}d`,[{pts,color:MCOL[m]||'#fb923c',band:true}],MUNIT[m]));
      }
      if(!mets.length)box.innerHTML='<div class="loading">No summary data yet for this range.</div>';
    }
  }catch(e){
    if(CURRENT===id)box.innerHTML='<div class="loading">Error loading history: '+e.message+'</div>';
  }
}

// ── Self-contained SVG line/band chart (no external libs, offline-first) ──
function chartCard(title,series,unit,spanSec){
  spanSec=spanSec||RANGE;
  const W=920,H=210,P={l:42,r:14,t:14,b:24};
  const all=series.flatMap(s=>s.pts);
  const card=document.createElement('div');
  card.className='chart-box';
  if(!all.length){
    card.innerHTML=`<div class="chart-title"><span>${title}</span></div>
      <div class="loading" style="padding:.5rem">No data.</div>`;
    return card;
  }
  const tMin=Math.min(...all.map(p=>p.t)), tMax=Math.max(...all.map(p=>p.t));
  let vMin=Math.min(...all.map(p=>p.lo!=null?p.lo:p.v));
  let vMax=Math.max(...all.map(p=>p.hi!=null?p.hi:p.v));
  const pad=(vMax-vMin)*0.1||1; vMin-=pad; vMax+=pad;
  const sx=t=>P.l+(tMax===tMin?0:(t-tMin)/(tMax-tMin))*(W-P.l-P.r);
  const sy=v=>P.t+(1-(v-vMin)/(vMax-vMin))*(H-P.t-P.b);
  const cur=series[0].pts.length?series[0].pts[series[0].pts.length-1].v:null;

  // y gridlines (4)
  let grid='',labels='';
  for(let i=0;i<=4;i++){
    const v=vMin+(vMax-vMin)*i/4, y=sy(v);
    grid+=`<line class="grid-l" x1="${P.l}" y1="${y.toFixed(1)}" x2="${W-P.r}" y2="${y.toFixed(1)}"/>`;
    labels+=`<text class="lbl" x="${P.l-6}" y="${(y+3).toFixed(1)}" text-anchor="end">${v.toFixed(0)}</text>`;
  }
  // x labels (start, mid, end)
  const fmt=t=>{const d=new Date(t);return spanSec<=172800
    ?d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})
    :(d.getMonth()+1)+'/'+d.getDate();};
  let xlab='';
  [tMin,(tMin+tMax)/2,tMax].forEach((t,i)=>{
    xlab+=`<text class="lbl" x="${sx(t).toFixed(1)}" y="${H-6}" text-anchor="${i===0?'start':i===2?'end':'middle'}">${fmt(t)}</text>`;
  });

  let paths='';
  for(const s of series){
    if(s.band&&s.pts.some(p=>p.lo!=null)){
      const up=s.pts.map(p=>`${sx(p.t).toFixed(1)},${sy(p.hi).toFixed(1)}`).join(' ');
      const dn=s.pts.slice().reverse().map(p=>`${sx(p.t).toFixed(1)},${sy(p.lo).toFixed(1)}`).join(' ');
      paths+=`<polygon class="band" points="${up} ${dn}" fill="${s.color}"/>`;
    }
    const line=s.pts.map((p,i)=>`${i?'L':'M'}${sx(p.t).toFixed(1)} ${sy(p.v).toFixed(1)}`).join(' ');
    paths+=`<path d="${line}" fill="none" stroke="${s.color}" stroke-width="1.6"/>`;
    // full-point markers for raw mode when not too dense to read
    if(s.dots && s.pts.length<=800)
      for(const p of s.pts) paths+=`<circle cx="${sx(p.t).toFixed(1)}" cy="${sy(p.v).toFixed(1)}" r="1.7" fill="${s.color}"/>`;
  }

  card.innerHTML=`<div class="chart-title"><span>${title}</span>
      <span class="chart-cur">${cur!=null?cur.toFixed(1)+' '+unit:''}</span></div>
    <svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="xMidYMid meet">
      ${grid}
      <line class="axis" x1="${P.l}" y1="${P.t}" x2="${P.l}" y2="${H-P.b}"/>
      <line class="axis" x1="${P.l}" y1="${H-P.b}" x2="${W-P.r}" y2="${H-P.b}"/>
      ${paths}${labels}${xlab}
    </svg>`;
  return card;
}

// ── Composable graphs workspace ───────────────────────────────────────────
const PALETTE=['#fb923c','#38bdf8','#4ade80','#f472b6','#a78bfa','#facc15','#fb7185','#2dd4bf'];
const MLABEL={temperature_c:'temp',humidity_pct:'humidity',dew_point_c:'dew pt',pressure_hpa:'pressure',pressure_msl_hpa:'pressure msl',radon_bqm3:'radon (Bq/m³)',battery_pct:'battery',co2_ppm:'CO₂'};
let GRAPHS=loadGraphs(), WX_METRICS=[];

function loadGraphs(){try{return JSON.parse(localStorage.getItem('ha_graphs')||'[]')}catch{return[]}}
function saveGraphs(){localStorage.setItem('ha_graphs',JSON.stringify(GRAPHS))}
function gid(){return 'g'+Math.random().toString(36).slice(2,8)}
function graphsActive(){return document.getElementById('graphs').classList.contains('show')}

function toggleView(){
  if(graphsActive()){
    document.getElementById('graphs').classList.remove('show');
    document.getElementById('grid').classList.remove('hide');
    document.getElementById('hdr').textContent='Home Sensors';
    document.getElementById('navbtn').textContent='📊 Graphs';
    refresh();
  }else{
    CURRENT=null;
    document.getElementById('detail').classList.remove('show');
    document.getElementById('detail').innerHTML='';
    document.getElementById('grid').classList.add('hide');
    document.getElementById('graphs').classList.add('show');
    document.getElementById('hdr').textContent='Graphs';
    document.getElementById('navbtn').textContent='← Sensors';
    renderGraphs();
  }
}

async function ensureMeta(){
  if(!DEVICES.length){try{DEVICES=await(await fetch('/devices')).json();}catch{}}
  if(!WX_METRICS.length){try{const m=await(await fetch('/weather/meta')).json();if(m.available)WX_METRICS=m.metrics;}catch{}}
}
function metricsFor(src){
  if(src==='weather')return WX_METRICS.length?WX_METRICS:['temperature_c','humidity_pct','pressure_hpa'];
  const d=DEVICES.find(x=>x.device_id===src);   // offer the device's REAL metrics (radon, pressure, …)
  return (d&&d.metrics&&d.metrics.length)?d.metrics:['temperature_c','humidity_pct','dew_point_c'];
}

function addGraph(){GRAPHS.push({id:gid(),title:'New graph',range:86400,series:[]});saveGraphs();renderGraphs();}
function removeGraph(id){GRAPHS=GRAPHS.filter(g=>g.id!==id);saveGraphs();renderGraphs();}
function setGraphTitle(id,t){const g=GRAPHS.find(x=>x.id===id);if(g){g.title=t;saveGraphs();}}
function setGraphRange(id,s){const g=GRAPHS.find(x=>x.id===id);if(g){g.range=s;saveGraphs();renderGraphs();}}
function removeSeries(id,i){const g=GRAPHS.find(x=>x.id===id);if(g){g.series.splice(i,1);saveGraphs();renderGraphs();}}
function onSrcChange(id){const src=document.getElementById('src-'+id).value;
  document.getElementById('met-'+id).innerHTML=src?metricsFor(src).map(m=>`<option value="${m}">${MLABEL[m]||m}</option>`).join(''):'';}
function addSeriesFrom(id){const src=document.getElementById('src-'+id).value,met=document.getElementById('met-'+id).value;
  if(!src||!met)return;const g=GRAPHS.find(x=>x.id===id);
  g.series.push({source:src,metric:met,color:PALETTE[g.series.length%PALETTE.length]});saveGraphs();renderGraphs();}

function srcOptions(){return `<option value="">+ series…</option>`+
  DEVICES.map(d=>`<option value="${d.device_id}">${titleCase(d.area)} · ${d.device_id}</option>`).join('')+
  `<option value="weather">Weather (home)</option>`;}
function presets(g){return [[3600,'1h'],[21600,'6h'],[86400,'24h'],[604800,'7d'],[2592000,'30d'],[7776000,'90d']]
  .map(([s,l])=>`<button class="rbtn${g.range===s?' active':''}" onclick="setGraphRange('${g.id}',${s})">${l}</button>`).join('');}
function graphCardHTML(g){
  const chips=g.series.map((s,i)=>{const lbl=(s.source==='weather'?'weather':s.source)+' '+(MLABEL[s.metric]||s.metric);
    return `<span class="chip"><span class="sw" style="background:${s.color}"></span>${lbl}<span class="x" onclick="removeSeries('${g.id}',${i})">✕</span></span>`;}).join('');
  return `<div class="gcard">
    <div class="ghead"><input class="gtitle" value="${(g.title||'').replace(/"/g,'&quot;')}" onchange="setGraphTitle('${g.id}',this.value)">
      <span class="del" onclick="removeGraph('${g.id}')">remove ✕</span></div>
    <div class="ranges">${presets(g)}</div>
    <div class="legend">${chips||'<span style="color:#475569;font-size:.75rem">no series yet</span>'}</div>
    <div class="addrow">
      <select class="sel" id="src-${g.id}" onchange="onSrcChange('${g.id}')">${srcOptions()}</select>
      <select class="sel" id="met-${g.id}"></select>
      <button class="rbtn" onclick="addSeriesFrom('${g.id}')">add</button></div>
    <div id="chart-${g.id}"><div class="loading">${g.series.length?'Loading…':'Add a series to plot.'}</div></div>
  </div>`;
}
async function renderGraphs(){
  await ensureMeta();
  const box=document.getElementById('graphs');
  box.innerHTML=(GRAPHS.map(graphCardHTML).join('')||'<div class="loading">No graphs yet.</div>')
    +`<button class="navbtn" onclick="addGraph()">+ Add graph</button>`;
  GRAPHS.forEach(loadGraphData);
}

function dewpoint(tC,rh){const a=17.625,b=243.04,al=Math.log(Math.max(rh,1)/100)+a*tC/(b+tC);return (b*al)/(a-al);}
async function fetchSeries(source,metric,sISO,eISO){
  const toV=(m,v)=>m==='temperature_c'||m==='dew_point_c'?fToScale(v):v;
  if(source==='weather'){
    const d=await(await fetch(`/weather/readings?metric=${metric}&start=${sISO}&end=${eISO}&limit=50000`)).json();
    return (d.readings||[]).map(r=>({t:+new Date(r.ts),v:toV(metric,r.value)}));
  }
  if(metric==='dew_point_c'){
    const [td,hd]=await Promise.all([
      fetch(`/devices/${source}/readings?metric=temperature_c&start=${sISO}&end=${eISO}&limit=50000`).then(r=>r.json()),
      fetch(`/devices/${source}/readings?metric=humidity_pct&start=${sISO}&end=${eISO}&limit=50000`).then(r=>r.json())]);
    const hm=new Map((hd.readings||[]).map(r=>[r.ts,r.value])),pts=[];
    for(const r of (td.readings||[])){const h=hm.get(r.ts);if(h!=null)pts.push({t:+new Date(r.ts),v:fToScale(dewpoint(r.value,h))});}
    return pts;
  }
  const d=await(await fetch(`/devices/${source}/readings?metric=${metric}&start=${sISO}&end=${eISO}&limit=50000`)).json();
  return (d.readings||[]).map(r=>({t:+new Date(r.ts),v:toV(metric,r.value)}));
}
async function loadGraphData(g){
  const el=document.getElementById('chart-'+g.id);
  if(!el)return;
  if(!g.series.length){el.innerHTML='<div class="loading">Add a series to plot.</div>';return;}
  const end=new Date(),start=new Date(Date.now()-g.range*1000);
  const sISO=start.toISOString().slice(0,19)+'Z',eISO=end.toISOString().slice(0,19)+'Z';
  try{
    const series=await Promise.all(g.series.map(async s=>({pts:await fetchSeries(s.source,s.metric,sISO,eISO),color:s.color})));
    const has=series.some(s=>s.pts.length);
    el.innerHTML='';
    if(!has){el.innerHTML='<div class="loading">No data in this range.</div>';return;}
    el.appendChild(chartCard('',series.filter(s=>s.pts.length),'',g.range));
  }catch(e){el.innerHTML='<div class="loading">Error: '+(e.message||e)+'</div>';}
}

document.addEventListener('keydown',e=>{if(e.key==='Escape'&&CURRENT)closeDetail()});
refresh();
setInterval(refresh,REFRESH);
</script>
</body>
</html>"""


@app.get("/health")
def health():
    try:
        conn = _hot_conn()
        row = conn.execute(
            "SELECT COUNT(*) as rows, MAX(ts) as latest FROM readings"
        ).fetchone()
        conn.close()
        return {
            "status": "ok",
            "hot_rows": row["rows"],
            "latest_reading": row["latest"],
        }
    except Exception as exc:
        return JSONResponse(status_code=503, content={"status": "error", "detail": str(exc)})


def _control_conn() -> Optional[sqlite3.Connection]:
    """Open control.db read-only-ish (None if it doesn't exist yet — controller never started)."""
    if not CONTROL_DB.exists():
        return None
    from server.control import control_store as store
    conn = sqlite3.connect(str(CONTROL_DB))
    store.ensure_schema(conn)
    return conn


def _device_meta() -> dict:
    """User overlay {device_id: {name, room, hidden}} from control.db (R8); {} if unavailable."""
    cc = _control_conn()
    if cc is None:
        return {}
    from server.control import control_store as store
    try:
        return store.all_device_meta(cc)
    finally:
        cc.close()


@app.get("/api/v1/sensors", include_in_schema=True)
def sensor_list():
    """All trusted sensors with latest values per metric (one call for the dashboard). Read-only."""
    import time

    from server.api.viewmodel import build_sensor_list
    hc = _hot_conn() if DB_PATH.exists() else None
    try:
        sensors = build_sensor_list(hc, time.time(), meta=_device_meta())
    finally:
        if hc is not None:
            hc.close()
    return {"sensors": sensors}


@app.get("/api/v1/alerts", include_in_schema=True)
def alerts():
    """Active alerts (low battery / unreachable / tank-full / override-expiring). Read-only; the single
    source of alert rules for the web app, MCU panels, and future push."""
    import time

    from server.api.viewmodel import build_alerts, build_display, build_sensor_list
    from server.control import control_store as store
    now = time.time()
    hc = _hot_conn() if DB_PATH.exists() else None
    cc = _control_conn()
    try:
        meta = store.all_device_meta(cc) if cc is not None else {}
        sensors = build_sensor_list(hc, now, meta=meta) if hc is not None else []
        displays = []
        if cc is not None:
            reg = getattr(app.state, "control_registry", None)
            displays = [vm for did in sorted(store.all_policies(cc))
                        if (vm := build_display(cc, hc, did, now, registry=reg, meta=meta)) is not None]
        return {"alerts": build_alerts(sensors, displays, now)}
    finally:
        if hc is not None:
            hc.close()
        if cc is not None:
            cc.close()


@app.get("/api/v1/displays", include_in_schema=True)
def display_list():
    """All controllable devices as render-ready view-models (one call for the dashboard). Read-only."""
    import time

    from server.api.viewmodel import build_display
    from server.control import control_store as store
    cc = _control_conn()
    if cc is None:
        return {"devices": []}
    hc = _hot_conn() if DB_PATH.exists() else None
    reg = getattr(app.state, "control_registry", None)
    dm = store.all_device_meta(cc)
    now = time.time()
    try:
        ids = sorted(store.all_policies(cc).keys())
        out = [vm for did in ids
               if (vm := build_display(cc, hc, did, now, registry=reg, meta=dm)) is not None]
    finally:
        cc.close()
        if hc is not None:
            hc.close()
    return {"devices": out}


@app.get("/api/v1/display/{device_id}", include_in_schema=True)
def display_viewmodel(device_id: str):
    """Render-ready snapshot for a constrained display (ADR-0013 BFF). Read-only, unauthenticated like
    the rest of the dashboard — composes control.db (policy/override/decision) + hot.db (the sensor
    driving the loop + the device's own non-authoritative read) into one flat object + a health word."""
    import time

    from server.api.viewmodel import build_display
    cc = _control_conn()
    if cc is None:
        raise HTTPException(status_code=503, detail="control state not available")
    hc = _hot_conn() if DB_PATH.exists() else None
    reg = getattr(app.state, "control_registry", None)
    try:
        from server.control import control_store as store
        vm = build_display(cc, hc, device_id, time.time(), registry=reg, meta=store.all_device_meta(cc))
    finally:
        cc.close()
        if hc is not None:
            hc.close()
    if vm is None:
        raise HTTPException(status_code=404, detail=f"no controllable device {device_id!r}")
    return vm


@app.get("/devices")
def list_devices():
    """All known devices with their last reading timestamp and last RSSI."""
    conn = _hot_conn()
    try:
        rows = conn.execute(
            """SELECT d.device_id, d.device_type, d.area, d.last_ts, d.last_rssi,
                      GROUP_CONCAT(DISTINCT r.metric) as metrics
               FROM device_last_seen d
               LEFT JOIN readings r ON r.device_id = d.device_id
               GROUP BY d.device_id"""
        ).fetchall()
        result = []
        for row in rows:
            result.append({
                "device_id": row["device_id"],
                "device_type": row["device_type"],
                "area": row["area"],
                "last_ts": row["last_ts"],
                "last_rssi": row["last_rssi"],
                "metrics": row["metrics"].split(",") if row["metrics"] else [],
            })
        return result
    finally:
        conn.close()


@app.get("/devices/{device_id}/last")
def device_last(device_id: str):
    """Most recent value for every metric of a device."""
    conn = _hot_conn()
    try:
        rows = conn.execute(
            """SELECT metric, value, unit, ts
               FROM readings
               WHERE device_id = ?
               GROUP BY metric
               HAVING ts = MAX(ts)""",
            (device_id,),
        ).fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail=f"Device '{device_id}' not found")
        return {
            "device_id": device_id,
            "readings": [dict(r) for r in rows],
        }
    finally:
        conn.close()


@app.get("/devices/{device_id}/summary")
def device_summary(
    device_id: str,
    days: int = Query(default=7, ge=1, le=365),
):
    """
    Daily summary rows from the summaries table (precomputed by compactor).
    Falls back to live aggregation over hot SQLite for 'today' data not yet compacted.
    """
    conn = _hot_conn()
    try:
        # Ensure summaries table exists (may not if compactor hasn't run yet)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS summaries (
               date TEXT NOT NULL, device_id TEXT NOT NULL, metric TEXT NOT NULL,
               min_val REAL, max_val REAL, mean_val REAL, median_val REAL,
               count INTEGER, last_val REAL, last_ts TEXT,
               PRIMARY KEY (date, device_id, metric))"""
        )
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            """SELECT date, metric, min_val, max_val, mean_val, median_val, count, last_val, last_ts
               FROM summaries WHERE device_id = ? AND date >= ? ORDER BY date, metric""",
            (device_id, since),
        ).fetchall()

        # Supplement with live hot-tier aggregation for today (compactor runs overnight)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        live_rows = conn.execute(
            """SELECT metric,
                      MIN(value) as min_val, MAX(value) as max_val,
                      AVG(value) as mean_val, COUNT(*) as count,
                      MAX(ts) as last_ts
               FROM readings
               WHERE device_id = ? AND ts >= ?
               GROUP BY metric""",
            (device_id, f"{today}T00:00:00Z"),
        ).fetchall()

        result = [dict(r) for r in rows]
        if live_rows:
            for r in live_rows:
                result.append({
                    "date": today,
                    "metric": r["metric"],
                    "min_val": r["min_val"],
                    "max_val": r["max_val"],
                    "mean_val": r["mean_val"],
                    "median_val": None,  # not computed live
                    "count": r["count"],
                    "last_val": None,
                    "last_ts": r["last_ts"],
                    "live": True,
                })

        if not result:
            raise HTTPException(status_code=404, detail=f"No data for '{device_id}'")
        return {"device_id": device_id, "days": days, "summary": result}
    finally:
        conn.close()


@app.get("/devices/{device_id}/readings")
async def device_readings(
    device_id: str,
    start: str = Query(..., description="ISO 8601 UTC, e.g. 2026-06-01T00:00:00Z"),
    end: str = Query(..., description="ISO 8601 UTC, e.g. 2026-06-19T23:59:59Z"),
    metric: Optional[str] = Query(default=None),
    limit: int = Query(default=10000, ge=1, le=MAX_DEEP_ROWS),
):
    """
    Raw readings over a time range. Hot tier via sqlite3; Parquet archive via DuckDB's
    native reader (no sqlite_scan extension → works offline, no network/extension dir).
    Bounded by MAX_DEEP_ROWS and serialised to prevent concurrent heavy queries.
    """
    async with _deep_query_lock:
        return await asyncio.to_thread(
            _deep_query, device_id, start, end, metric, limit
        )


_COLS = ["ts", "device_id", "metric", "value", "unit", "area", "transport"]


def _deep_query(
    device_id: str,
    start: str,
    end: str,
    metric: Optional[str],
    limit: int,
) -> dict:
    import glob as _glob

    rows: list[dict] = []

    # ── Hot tier (today's live data) via plain sqlite3 — no DuckDB extension ──
    # hot_keys lets the hot tier WIN on any (ts, metric) collision with parquet. Re-imports (e.g. a
    # timezone-corrected CSV) land in hot for already-compacted dates; without this the API returned
    # both the stale parquet row AND the corrected hot row at the same timestamp → a duplicate
    # "band" on the graph. Hot = most-recent write = authoritative.
    hot_keys: set = set()
    if DB_PATH.exists():
        conn = sqlite3.connect(str(DB_PATH))
        try:
            q = f"SELECT {','.join(_COLS)} FROM readings WHERE device_id=? AND ts>=? AND ts<=?"
            params: list = [device_id, start, end]
            if metric:
                q += " AND metric=?"
                params.append(metric)
            for r in conn.execute(q, params).fetchall():
                d = dict(zip(_COLS, r))
                rows.append(d)
                hot_keys.add((d["ts"], d["metric"]))
        finally:
            conn.close()

    # ── Cold tier (Parquet archive) via DuckDB native read_parquet ──
    pattern = _parquet_glob()
    if _glob.glob(pattern):
        try:
            duck = duckdb.connect()
            # Keep DuckDB's scratch under a writable dir (service home is read-only)
            duck.execute(f"SET home_directory='{PARQUET_GLOB}'")
            q = (
                f"SELECT {','.join(_COLS)} FROM read_parquet(?, hive_partitioning=true, "
                f"union_by_name=true) WHERE device_id=? AND ts>=? AND ts<=?"
            )
            params = [pattern, device_id, start, end]
            if metric:
                q += " AND metric=?"
                params.append(metric)
            for r in duck.execute(q, params).fetchall():
                d = dict(zip(_COLS, r))
                if (d["ts"], d["metric"]) in hot_keys:
                    continue                       # hot wins — drop the stale parquet duplicate
                rows.append(d)
            duck.close()
        except Exception as exc:
            log.error("DuckDB parquet query failed: %s", exc)
            raise HTTPException(status_code=500, detail=f"Query error: {exc}")

    rows.sort(key=lambda r: r["ts"])
    n = len(rows)
    truncated = n > limit
    if truncated:
        # Decimate evenly across the WHOLE range (keep first & last) rather than
        # slicing off the tail — otherwise dense long-range series lose their most
        # recent data and render only partway across the time axis.
        idx = sorted({round(i * (n - 1) / (limit - 1)) for i in range(limit)})
        rows = [rows[i] for i in idx]

    return {
        "device_id": device_id,
        "start": start,
        "end": end,
        "metric": metric,
        "rows": len(rows),
        "truncated": truncated,
        "readings": rows,
    }


@app.get("/areas")
def list_areas():
    """Distinct areas from device_last_seen."""
    conn = _hot_conn()
    try:
        rows = conn.execute(
            "SELECT DISTINCT area FROM device_last_seen ORDER BY area"
        ).fetchall()
        return [r["area"] for r in rows]
    finally:
        conn.close()


# ── Weather lane (separate weather.db) ──────────────────────────────────────────

def _weather_conn() -> Optional[sqlite3.Connection]:
    if not WEATHER_DB.exists():
        return None
    conn = sqlite3.connect(str(WEATHER_DB), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/weather/meta")
def weather_meta():
    """Locations + metrics available in the weather lane (for the dashboard series picker)."""
    conn = _weather_conn()
    if conn is None:
        return {"available": False, "locations": [], "metrics": []}
    try:
        locs = [r[0] for r in conn.execute(
            f"SELECT DISTINCT location FROM {WEATHER_TABLE} ORDER BY location")]
        mets = [r[0] for r in conn.execute(
            f"SELECT DISTINCT metric FROM {WEATHER_TABLE} ORDER BY metric")]
        return {"available": True, "locations": locs, "metrics": mets}
    finally:
        conn.close()


@app.get("/weather/readings")
def weather_readings(
    metric: str = Query(..., description="e.g. temperature_c, humidity_pct, pressure_hpa"),
    start: str = Query(..., description="ISO 8601 UTC"),
    end: str = Query(..., description="ISO 8601 UTC"),
    location: Optional[str] = Query(default=None),
    limit: int = Query(default=20000, ge=1, le=MAX_DEEP_ROWS),
):
    """Weather time-series for a metric/range — for graphing alongside sensor data."""
    conn = _weather_conn()
    if conn is None:
        return {"metric": metric, "rows": 0, "readings": []}
    try:
        q = (f"SELECT ts, value, unit, location FROM {WEATHER_TABLE} "
             "WHERE metric=? AND ts>=? AND ts<=?")
        params: list = [metric, start, end]
        if location:
            q += " AND location=?"
            params.append(location)
        q += " ORDER BY ts LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in conn.execute(q, params).fetchall()]
        return {"metric": metric, "rows": len(rows), "readings": rows}
    finally:
        conn.close()


# ── Dev runner ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
    uvicorn.run("server.api.main:app", host="0.0.0.0", port=8123, reload=False)
