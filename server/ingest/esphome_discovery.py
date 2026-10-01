"""Live ESPHome-node discovery cache — surfaces online-but-UNREGISTERED ESPHome appliances (e.g. a freshly
reflashed Levoit purifier) in the PWA "Add device → Standby hardware" list, beside ADR-0036's edge nodes.

Why a sibling of edge_discovery.py rather than more rows in it: an ESPHome appliance is not one of our edge
nodes. It never sends a `hello`, has no node-born secret to claim, and its placement truth is
levoit-devices.yaml + control.yaml (a server-driven actuator), not devices.yaml + an edge manifest. Same UI
list, different identity model — so a different module, same shape (cache + candidates + start_subscriber).

ESPHome (MQTT mode) publishes, retained by default:
  - <name>/status                 — availability birth/LWT: "online" | "offline"
  - <name>/<component>/<obj>/state — one topic per entity

There is no self-description, so the ability is INFERRED FROM WHAT THE NODE PUBLISHES (conform-by-ability,
docs/CONFORMANCE.md): a node with a fan entity AND a PM2.5 sensor is an air purifier. A node matching no
signature is never surfaced — we only list what intake can actually adopt.

Liveness follows the retained `status` (an online-but-quiet appliance must not age out); the broker's LWT
flips it to "offline", which removes it from the list.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path

log = logging.getLogger("ha.ingest.esphome_discovery")

REPO = Path(__file__).resolve().parents[2]
LEVOIT_REGISTRY = REPO / "instance" / "levoit-devices.yaml"   # ESPHome name -> device_id (placement truth)

# ESPHome node names are hostnames: lowercase alnum + '-' (and '_' in older configs). Anything else on a
# `+/status` topic is not an ESPHome node — ignore it rather than surface junk.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

# ability -> the entity topic suffixes whose presence identifies it. ALL must be seen.
ABILITY_SIGNATURES: dict[str, frozenset[str]] = {
    "air_purifier": frozenset({"fan/fan/state", "sensor/pm_2_5/state"}),
}
SIGNATURE_SUFFIXES = sorted({s for sig in ABILITY_SIGNATURES.values() for s in sig})
STATUS_TOPIC = "+/status"
OFFLINE_TTL_S = 24 * 3600


def load_registered_names(path: Path = LEVOIT_REGISTRY) -> set[str]:
    """ESPHome names already mapped to a device_id. Best-effort: unreadable -> empty (fail OPEN to listing;
    the intake handler re-checks the file before writing, so a stale read can never double-register)."""
    try:
        import yaml
        return {str(k) for k in (yaml.safe_load(path.read_text()) or {})}
    except FileNotFoundError:
        return set()
    except Exception:
        log.debug("esphome_discovery: %s unreadable", path, exc_info=True)
        return set()


def classify(entities: set[str]) -> list[str]:
    """The abilities a node's published entity suffixes satisfy (sorted, possibly empty)."""
    return sorted(a for a, sig in ABILITY_SIGNATURES.items() if sig <= entities)


def _split(topic: str) -> tuple[str | None, str]:
    name, _, suffix = topic.partition("/")
    return (name if NAME_RE.match(name or "") else None), suffix


class EsphomeDiscoveryCache:
    """Thread-safe {name: row}. Written by the MQTT thread, read by API handlers."""

    def __init__(self, offline_ttl_s: int = OFFLINE_TTL_S):
        self._ttl = offline_ttl_s
        self._by_name: dict[str, dict] = {}
        self._lock = threading.Lock()

    def ingest(self, topic: str, payload: str, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        name, suffix = _split(topic)
        if not name or not suffix:
            return
        with self._lock:
            row = self._by_name.setdefault(name, {"node": name, "first_seen": now, "entities": set()})
            row["last_seen"] = now
            if suffix == "status":
                row["online"] = (payload or "").strip().lower() == "online"
            else:
                row["entities"].add(suffix)

    def row(self, name: str) -> dict | None:
        with self._lock:
            r = self._by_name.get(name)
            return {**r, "entities": set(r["entities"])} if r else None

    def candidates(self, *, registered: set[str] | None = None, now: float | None = None) -> list[dict]:
        """Online, ability-classified, not-yet-registered ESPHome nodes, newest first. Shaped like
        edge_discovery rows (node/abilities/age_s) plus kind='esphome' so the PWA routes the adopt."""
        now = time.time() if now is None else now
        registered = load_registered_names() if registered is None else registered
        with self._lock:
            for n in [n for n, r in self._by_name.items()
                      if not r.get("online") and now - r.get("last_seen", now) > self._ttl]:
                del self._by_name[n]
            rows = [{**r, "entities": set(r["entities"])} for r in self._by_name.values()]
        out = []
        for r in rows:
            if not r.get("online") or r["node"] in registered:
                continue
            abilities = classify(r["entities"])
            if not abilities:
                continue
            out.append({"node": r["node"], "kind": "esphome", "chip": "esphome", "abilities": abilities,
                        "online": True, "known": False, "last_seen": r["last_seen"],
                        "age_s": round(now - r["last_seen"], 1)})
        out.sort(key=lambda r: r["last_seen"], reverse=True)
        return out


def start_subscriber(cache: EsphomeDiscoveryCache, *, broker: str = "localhost", port: int = 1883):
    """Best-effort paho subscriber feeding `cache`. Returns the client, or None if paho/broker unavailable —
    the ESPHome half of the standby list then stays empty and nothing else is affected (mirrors
    edge_discovery.start_subscriber)."""
    try:
        import paho.mqtt.client as mqtt
        from server.util.mqtt_creds import apply_credentials
    except Exception:
        log.warning("esphome_discovery: paho unavailable — ESPHome intake candidates will stay empty")
        return None

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    apply_credentials(client)

    def on_connect(c, userdata, flags, rc, properties=None):
        if rc == 0:
            c.subscribe([(STATUS_TOPIC, 0)] + [(f"+/{s}", 0) for s in SIGNATURE_SUFFIXES])
            log.info("esphome_discovery subscribed to status + %d signature topic(s) on %s:%s",
                     len(SIGNATURE_SUFFIXES), broker, port)
        else:
            log.warning("esphome_discovery MQTT connect failed rc=%s", rc)

    def on_message(c, userdata, msg):
        try:
            cache.ingest(msg.topic, msg.payload.decode(errors="replace"))
        except Exception:
            log.debug("esphome_discovery: bad message on %s", msg.topic, exc_info=True)

    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(broker, port, keepalive=60)
    except Exception:
        log.warning("esphome_discovery: broker %s:%s unreachable — disabled this cycle", broker, port,
                    exc_info=True)
        return None
    client.loop_start()
    return client
