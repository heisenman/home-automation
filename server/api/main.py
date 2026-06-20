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
</style>
</head>
<body>
<header>
  <h1>Home Sensors</h1>
  <span id="ts"></span>
</header>
<div class="grid" id="grid"><div style="color:#475569;padding:2rem">Loading…</div></div>
<script>
const REFRESH=30000;
const toF=c=>(c*9/5+32).toFixed(1);
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

function card(dev,last){
  const rs=last?last.readings:[];
  const tc=getM(rs,'temperature_c');
  const hum=getM(rs,'humidity_pct');
  const bat=getM(rs,'battery_pct');
  const ts=lastTs(rs)||dev.last_ts;
  const ageS=ts?(Date.now()-new Date(ts).getTime())/1000:Infinity;
  const stale=ageS>300;
  const area=dev.area.replace(/_/g,' ').replace(/\\b\\w/g,c=>c.toUpperCase());
  const batLabel=bat!==null&&bat!==undefined?(bat>100?bat+'%*':bat+'%'):'';

  return`<div class="card${stale?' stale':''}">
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
  try{
    const devs=await(await fetch('/devices')).json();
    devs.sort((a,b)=>a.area.localeCompare(b.area));
    const lasts=await Promise.all(devs.map(async d=>{
      try{const r=await fetch('/devices/'+d.device_id+'/last');return r.ok?r.json():null}
      catch{return null}
    }));
    document.getElementById('grid').innerHTML=devs.map((d,i)=>card(d,lasts[i])).join('');
    document.getElementById('ts').textContent='updated '+new Date().toLocaleTimeString();
  }catch(e){console.error(e)}
}
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
    Raw readings over a time range — DuckDB queries both hot SQLite and Parquet archive.
    Bounded by MAX_DEEP_ROWS and serialised to prevent concurrent heavy queries.
    """
    async with _deep_query_lock:
        return await asyncio.to_thread(
            _deep_query, device_id, start, end, metric, limit
        )


def _deep_query(
    device_id: str,
    start: str,
    end: str,
    metric: Optional[str],
    limit: int,
) -> dict:
    parquet_pattern = _parquet_glob()
    hot_db = str(DB_PATH)

    metric_filter = f"AND metric = '{metric}'" if metric else ""

    sql = f"""
        WITH hot AS (
            SELECT ts, device_id, metric, value, unit, area, transport
            FROM sqlite_scan('{hot_db}', 'readings')
            WHERE device_id = '{device_id}'
              AND ts >= '{start}' AND ts <= '{end}'
              {metric_filter}
        ),
        cold AS (
            SELECT ts, device_id, metric, value, unit, area, transport
            FROM read_parquet('{parquet_pattern}', hive_partitioning=true, union_by_name=true)
            WHERE device_id = '{device_id}'
              AND ts >= '{start}' AND ts <= '{end}'
              {metric_filter}
        )
        SELECT * FROM hot
        UNION ALL
        SELECT * FROM cold
        ORDER BY ts
        LIMIT {limit}
    """

    try:
        duck = duckdb.connect()
        duck.install_extension("sqlite")
        duck.load_extension("sqlite")
        result = duck.execute(sql).fetchdf()
        duck.close()
    except Exception as exc:
        log.error("DuckDB query failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Query error: {exc}")

    return {
        "device_id": device_id,
        "start": start,
        "end": end,
        "metric": metric,
        "rows": len(result),
        "truncated": len(result) == limit,
        "readings": result.to_dict(orient="records"),
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
