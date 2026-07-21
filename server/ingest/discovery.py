"""Live BLE discovery cache — surfaces UNREGISTERED SwitchBot adverts for the PWA "Add sensor" flow.

ha-scanner publishes decode-able-but-unregistered devices (a brand-new meter, or a rotating BLE address)
to home/unknown/<mac>/raw instead of the canonical grid, so they don't spawn junk tiles. This module
keeps a short rolling cache of those candidates so the API can answer GET /api/v1/discover instantly even
though the raw topic is ephemeral (QoS 0, retain=False, debounced ~60s/mac by the scanner).

Decode reuses the real switchbot decoder — the exact bytes ha-scanner would have published as canonical
state had the MAC been registered. This is the server half of tools/intake_sensor.py (the CLI reference
implementation); both import decode_raw() so the two paths never drift.
"""
from __future__ import annotations

import json
import logging
import threading
import time

from server.ingest.decoders import switchbot

log = logging.getLogger("ha.ingest.discovery")

CANDIDATE_TTL_S = 300            # drop a candidate this long after its last advert
DISCOVERY_TOPIC = "home/unknown/+/raw"


def decode_raw(payload: dict) -> dict | None:
    """Decode a home/unknown/<mac>/raw payload into {device_type, temperature_c, humidity_pct,
    battery_pct?} via the real switchbot decoder, or None if it isn't a decodable SwitchBot advert.

    (Aranet candidates arrive on the same topic with brand='aranet'; their temp/hum decode isn't wired
    here, so they surface as raw-only with no metrics.)"""
    if not isinstance(payload, dict) or payload.get("brand") != "switchbot":
        return None
    try:
        mfr = {int(k): bytes.fromhex(v) for k, v in (payload.get("manufacturer_data") or {}).items()}
        svc = {k: bytes.fromhex(v) for k, v in (payload.get("service_data") or {}).items()}
    except (ValueError, TypeError, AttributeError):
        return None
    if not switchbot.is_switchbot(mfr, svc):
        return None
    result = switchbot.decode(str(payload.get("mac", "")), mfr, svc, int(payload.get("rssi", 0) or 0))
    if not result:
        return None
    out = {"device_type": result["device_type"]}
    out.update(result["metrics"])
    return out


class DiscoveryCache:
    """Thread-safe rolling cache of recently-heard UNREGISTERED BLE candidates, keyed by MAC. Written by
    the MQTT subscriber thread (ingest), read by API request handlers (candidates) — hence the lock."""

    def __init__(self, ttl_s: int = CANDIDATE_TTL_S):
        self._ttl = ttl_s
        self._by_mac: dict[str, dict] = {}
        self._lock = threading.Lock()

    def ingest(self, payload: dict, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        mac = str((payload or {}).get("mac", "")).upper()
        if not mac:
            return
        rssi = int(payload.get("rssi", 0) or 0)
        dec = decode_raw(payload)
        with self._lock:
            row = self._by_mac.get(mac)
            if row is None:
                row = {"mac": mac, "brand": payload.get("brand", "?"),
                       "first_seen": now, "count": 0, "rssi_max": rssi}
                self._by_mac[mac] = row
            row["last_seen"] = now
            row["rssi"] = rssi
            row["rssi_max"] = max(row.get("rssi_max", rssi), rssi)
            row["count"] += 1
            if dec:
                row.update(dec)     # device_type, temperature_c, humidity_pct, battery_pct

    def candidates(self, registry: dict | None = None, *, now: float | None = None) -> list[dict]:
        """Live (non-expired) candidates, strongest signal first, annotated for the UI. Skips MACs already
        present in `registry` (its keys are uppercase MACs) so a just-registered device drops off the list."""
        now = time.time() if now is None else now
        known = {str(k).upper() for k in (registry or {})}
        out: list[dict] = []
        with self._lock:
            for mac in [m for m, r in self._by_mac.items() if now - r["last_seen"] > self._ttl]:
                del self._by_mac[mac]                       # evict expired
            for mac, r in self._by_mac.items():
                if mac in known:
                    continue
                item = dict(r)
                t = item.get("temperature_c")
                if t is not None:
                    item["temperature_f"] = round(t * 9 / 5 + 32, 1)
                item["age_s"] = round(now - item["last_seen"], 1)
                out.append(item)
        out.sort(key=lambda r: r.get("rssi_max", -999), reverse=True)
        return out


def start_subscriber(cache: DiscoveryCache, *, broker: str = "localhost", port: int = 1883,
                     topic: str = DISCOVERY_TOPIC):
    """Best-effort paho subscriber that feeds `cache` from the broker's discovery topic. Returns the
    client (call loop_stop()/disconnect() on shutdown), or None if paho/broker is unavailable — in which
    case GET /discover simply stays empty and the rest of the API is unaffected."""
    try:
        import paho.mqtt.client as mqtt
        from server.util.mqtt_creds import apply_credentials
    except Exception:
        log.warning("discovery: paho unavailable — GET /discover will stay empty")
        return None

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    apply_credentials(client)

    def on_connect(c, userdata, flags, rc, properties=None):
        if rc == 0:
            c.subscribe(topic, qos=0)
            log.info("discovery subscribed to %s on %s:%s", topic, broker, port)
        else:
            log.warning("discovery MQTT connect failed rc=%s", rc)

    def on_message(c, userdata, msg):
        try:
            cache.ingest(json.loads(msg.payload.decode()))
        except Exception:
            log.debug("discovery: undecodable raw payload on %s", msg.topic, exc_info=True)

    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(broker, port, keepalive=60)
    except Exception:
        log.warning("discovery: broker %s:%s unreachable — discovery disabled this cycle",
                    broker, port, exc_info=True)
        return None
    client.loop_start()     # background thread handles reconnection
    return client
