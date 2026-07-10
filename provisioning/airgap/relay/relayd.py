#!/usr/bin/env python3
"""ha-relay — the air-gap internet relay daemon (ADR-0033).

Runs on the dual-homed bridge (.210). ha-2 (air-gapped) publishes a *request* on the dedicated relay broker;
this daemon fetches from a HARD egress allow-list and returns a schema-checked *response*. It is the internet-
facing edge of the trust boundary, so it is deliberately minimal and hostile-adjacent:

  * request/response only (ha-2 pulls on demand → stochastic egress; ADR-0033 §3.4)
  * a fixed service registry — each service maps to ONE allow-listed host; anything else is refused
  * per-service token-bucket rate limiting (a compromised ha-2 can't weaponize the relay to flood/exfil)
  * every request audited (id, service, host, status, bytes, ms)
  * never forwards arbitrary URLs; never an open proxy

Topics (on the dedicated relay broker, default localhost:1884):
  relay/<service>/request   {"id": "<uuid>", "params": {...}}          <- ha-2 publishes
  relay/<service>/response  {"id": "<uuid>", "ok": true, "data": {...}} -> daemon replies
                            {"id": "<uuid>", "ok": false, "error": "..."}

Env:
  RELAY_BROKER_HOST (localhost)  RELAY_BROKER_PORT (1884)
  RELAY_BROKER_USER / RELAY_BROKER_PASS (optional)
  RELAY_RATE_PER_MIN (30)        RELAY_WEATHER_LATLON ("lat,lon"; else from instance/weather.env)
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
import logging
import threading
import urllib.parse
import urllib.request
from collections import defaultdict

import paho.mqtt.client as mqtt

LOG = logging.getLogger("ha-relay")

# ---- HARD egress allow-list. A service may ONLY reach the host(s) named here. ----------------------
# (Deny-list is implicit: anything not here is refused. Never add vendor clouds / provisioning hosts —
#  see ADR-0033 §2.E.)
ALLOW = {
    "open-meteo.com", "api.open-meteo.com", "archive-api.open-meteo.com",
}

RATE_PER_MIN = int(os.environ.get("RELAY_RATE_PER_MIN", "30"))


def _latlon() -> tuple[str, str]:
    v = os.environ.get("RELAY_WEATHER_LATLON")
    if v and "," in v:
        a, b = v.split(",", 1)
        return a.strip(), b.strip()
    # fall back to the same source the .210 weather lane uses
    for p in ("instance/weather.env", os.path.expanduser("~/home_automation/instance/weather.env")):
        try:
            env = {}
            with open(p) as fh:
                for line in fh:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, val = line.split("=", 1)
                        env[k.strip()] = val.strip().strip('"').strip("'")
            lat = env.get("HA_WEATHER_LAT") or env.get("LAT")
            lon = env.get("HA_WEATHER_LON") or env.get("LON")
            if lat and lon:
                return lat, lon
        except FileNotFoundError:
            continue
    raise RuntimeError("no lat/lon (set RELAY_WEATHER_LATLON or instance/weather.env)")


def _get(url: str, timeout: float = 10.0) -> bytes:
    """GET with a hard allow-list check on the resolved host. Never follows to a non-allowed host."""
    host = urllib.parse.urlparse(url).hostname or ""
    if host not in ALLOW:
        raise PermissionError(f"egress host not allow-listed: {host!r}")
    req = urllib.request.Request(url, headers={"User-Agent": "ha-relay/1"})
    # No redirects to off-allow-list hosts: default opener follows redirects, so re-check via a handler.
    class _NoOffListRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            h = urllib.parse.urlparse(newurl).hostname or ""
            if h not in ALLOW:
                raise PermissionError(f"redirect to non-allow-listed host: {h!r}")
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    opener = urllib.request.build_opener(_NoOffListRedirect)
    with opener.open(req, timeout=timeout) as resp:
        return resp.read(1_000_000)  # 1MB cap — relay payloads are tiny


# ---- service handlers: each returns a JSON-able dict; each reaches only allow-listed hosts -----------
def _svc_weather(params: dict) -> dict:
    lat, lon = _latlon()
    q = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,surface_pressure,weather_code,wind_speed_10m",
        "timezone": "UTC",
    })
    raw = _get(f"https://api.open-meteo.com/v1/forecast?{q}")
    data = json.loads(raw)
    cur = data.get("current", {})
    return {
        "source": "open-meteo", "ts": cur.get("time"),
        "temperature_c": cur.get("temperature_2m"),
        "humidity_pct": cur.get("relative_humidity_2m"),
        "pressure_hpa": cur.get("surface_pressure"),
        "weather_code": cur.get("weather_code"),
        "wind_speed_ms": cur.get("wind_speed_10m"),
    }


HANDLERS = {"weather": _svc_weather}

# ---- per-service token-bucket rate limiting ---------------------------------------------------------
_lock = threading.Lock()
_buckets: dict[str, list] = defaultdict(lambda: [RATE_PER_MIN, time.monotonic()])


def _allow_rate(service: str) -> bool:
    with _lock:
        tokens, last = _buckets[service]
        now = time.monotonic()
        tokens = min(RATE_PER_MIN, tokens + (now - last) * (RATE_PER_MIN / 60.0))
        if tokens < 1:
            _buckets[service] = [tokens, now]
            return False
        _buckets[service] = [tokens - 1, now]
        return True


def _publish(client, service: str, payload: dict):
    client.publish(f"relay/{service}/response", json.dumps(payload), qos=1)


def _handle(client, service: str, body: bytes):
    t0 = time.monotonic()
    rid, status, nbytes = "?", "ok", 0
    try:
        msg = json.loads(body or b"{}")
        rid = str(msg.get("id") or uuid.uuid4())
        if service not in HANDLERS:
            raise KeyError(f"unknown service {service!r}")
        if not _allow_rate(service):
            raise RuntimeError("rate limited")
        data = HANDLERS[service](msg.get("params") or {})
        nbytes = len(json.dumps(data))
        _publish(client, service, {"id": rid, "ok": True, "data": data})
    except Exception as e:  # noqa: BLE001 — relay must never crash on a bad/hostile request
        status = f"err:{type(e).__name__}"
        _publish(client, service, {"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}"})
    finally:
        LOG.info("relay id=%s service=%s status=%s bytes=%d ms=%d",
                 rid, service, status, nbytes, int((time.monotonic() - t0) * 1000))


def _on_connect(client, _u, _f, rc, *_a):
    LOG.info("connected rc=%s; subscribing relay/+/request", rc)
    client.subscribe("relay/+/request", qos=1)


def _on_message(client, _u, msg):
    parts = msg.topic.split("/")
    if len(parts) == 3 and parts[0] == "relay" and parts[2] == "request":
        _handle(client, parts[1], msg.payload)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    host = os.environ.get("RELAY_BROKER_HOST", "localhost")
    port = int(os.environ.get("RELAY_BROKER_PORT", "1884"))
    # paho v1/v2 tolerant (shared-venv-esphome-paho): the daemon may run on a v1 or v2 paho.
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ha-relay")  # paho v2
    except (AttributeError, TypeError):
        client = mqtt.Client(client_id="ha-relay")  # paho v1
    u, p = os.environ.get("RELAY_BROKER_USER"), os.environ.get("RELAY_BROKER_PASS")
    if u:
        client.username_pw_set(u, p or "")
    client.on_connect = _on_connect
    client.on_message = _on_message
    LOG.info("ha-relay starting; broker=%s:%d services=%s allow=%s rate/min=%d",
             host, port, sorted(HANDLERS), sorted(ALLOW), RATE_PER_MIN)
    client.connect(host, port, keepalive=60)
    client.loop_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
