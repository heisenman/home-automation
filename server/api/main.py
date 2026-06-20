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
from fastapi.responses import HTMLResponse, JSONResponse

# ── Config ────────────────────────────────────────────────────────────────────

DB_PATH = Path(os.environ.get("HA_DB", "instance/db/hot.db"))
PARQUET_GLOB = Path(os.environ.get("HA_PARQUET_DIR", "instance/db/parquet"))
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
    allow_methods=["GET"],
    allow_headers=["*"],
)


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

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard():
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
</style>
</head>
<body>
<header>
  <h1 id="hdr">Home Sensors</h1>
  <span id="ts"></span>
</header>
<div class="grid" id="grid"><div style="color:#475569;padding:2rem">Loading…</div></div>
<div id="detail"></div>
<script>
const REFRESH=30000;
const toF=c=>(c*9/5+32).toFixed(1);
const fToScale=c=>c*9/5+32;
let DEVICES=[], CURRENT=null, RANGE=1;  // RANGE in days

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
      ${hum!==null?`<div class="m"><div class="ml">RH</div>${Math.round(hum)}%</div>`:''}
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
  try{
    const devs=await(await fetch('/devices')).json();
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
  CURRENT=id; RANGE=1;
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
function setRange(d){RANGE=d; renderDetail();}

function renderDetail(){
  const dev=DEVICES.find(d=>d.device_id===CURRENT)||{device_id:CURRENT,area:''};
  document.getElementById('hdr').textContent=titleCase(dev.area||CURRENT);
  const ranges=[[1,'24h'],[7,'7d'],[30,'30d'],[90,'90d']];
  document.getElementById('detail').innerHTML=`
    <button class="back" onclick="closeDetail()">← all sensors</button>
    <div class="dtitle">${titleCase(dev.area||CURRENT)}</div>
    <div class="dsub">${CURRENT}</div>
    <div class="ranges">${ranges.map(([d,l])=>
      `<button class="rbtn${RANGE===d?' active':''}" onclick="setRange(${d})">${l}</button>`).join('')}</div>
    <div id="charts"><div class="loading">Loading history…</div></div>`;
  loadCharts();
}

async function loadCharts(){
  const id=CURRENT, days=RANGE;
  const box=document.getElementById('charts');
  try{
    if(days<=1){
      // Intraday: raw readings
      const end=new Date(), start=new Date(Date.now()-days*864e5);
      const url=`/devices/${id}/readings?start=${start.toISOString().slice(0,19)}Z`
               +`&end=${end.toISOString().slice(0,19)}Z&limit=20000`;
      const data=await(await fetch(url)).json();
      if(CURRENT!==id)return;
      const rs=data.readings||[];
      const tSeries=rs.filter(r=>r.metric==='temperature_c').map(r=>({t:+new Date(r.ts),v:fToScale(r.value)}));
      const hSeries=rs.filter(r=>r.metric==='humidity_pct').map(r=>({t:+new Date(r.ts),v:r.value}));
      box.innerHTML='';
      box.appendChild(chartCard('Temperature (°F)',[{pts:tSeries,color:'#fb923c'}],'°F'));
      box.appendChild(chartCard('Humidity (%)',[{pts:hSeries,color:'#38bdf8'}],'%'));
      if(!tSeries.length&&!hSeries.length)box.innerHTML='<div class="loading">No data in this range.</div>';
    }else{
      // Multi-day: daily summary with min/max band + mean line
      const data=await(await fetch(`/devices/${id}/summary?days=${days}`)).json();
      if(CURRENT!==id)return;
      const sm=(data.summary||[]).filter(s=>!s.live);
      const tDay=sm.filter(s=>s.metric==='temperature_c').map(s=>({
        t:+new Date(s.date),lo:fToScale(s.min_val),hi:fToScale(s.max_val),v:fToScale(s.mean_val)}));
      const hDay=sm.filter(s=>s.metric==='humidity_pct').map(s=>({
        t:+new Date(s.date),lo:s.min_val,hi:s.max_val,v:s.mean_val}));
      box.innerHTML='';
      box.appendChild(chartCard('Temperature (°F) — daily min/avg/max',
        [{pts:tDay,color:'#fb923c',band:true}],'°F'));
      box.appendChild(chartCard('Humidity (%) — daily min/avg/max',
        [{pts:hDay,color:'#38bdf8',band:true}],'%'));
      if(!tDay.length&&!hDay.length)box.innerHTML='<div class="loading">No summary data yet for this range.</div>';
    }
  }catch(e){
    if(CURRENT===id)box.innerHTML='<div class="loading">Error loading history: '+e.message+'</div>';
  }
}

// ── Self-contained SVG line/band chart (no external libs, offline-first) ──
function chartCard(title,series,unit){
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
  const fmt=t=>{const d=new Date(t);return RANGE<=1
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
    if DB_PATH.exists():
        conn = sqlite3.connect(str(DB_PATH))
        try:
            q = f"SELECT {','.join(_COLS)} FROM readings WHERE device_id=? AND ts>=? AND ts<=?"
            params: list = [device_id, start, end]
            if metric:
                q += " AND metric=?"
                params.append(metric)
            rows.extend(dict(zip(_COLS, r)) for r in conn.execute(q, params).fetchall())
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
            rows.extend(dict(zip(_COLS, r)) for r in duck.execute(q, params).fetchall())
            duck.close()
        except Exception as exc:
            log.error("DuckDB parquet query failed: %s", exc)
            raise HTTPException(status_code=500, detail=f"Query error: {exc}")

    rows.sort(key=lambda r: r["ts"])
    truncated = len(rows) > limit
    rows = rows[:limit]

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


# ── Dev runner ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
    uvicorn.run("server.api.main:app", host="0.0.0.0", port=8123, reload=False)
