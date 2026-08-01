"""Live edge-NODE discovery cache (ADR-0036) — surfaces online-but-unassigned edge nodes for the PWA
"Add device → Standby hardware" intake flow.

Unlike discovery.py (which tracks unregistered SwitchBot BLE *adverts*), this tracks WiFi/MQTT edge *nodes*
that announce themselves on:
  - home/edge/<node>/hello   — retained self-describing identity (ADR-0036): {node,chip,mac,fw,slot,
                               enrolled,abilities}. Present on hello-capable firmware.
  - home/edge/<node>/status  — retained liveness LWT: "online <slot> <fw>" / "offline".

A node is an intake CANDIDATE when it is ONLINE and not yet assigned to a real area — i.e. its manifest
`area` is standby/unassigned, or it isn't in the manifest at all (brand-new hardware announcing hello).

Identity comes from the node's own `hello` when present; for already-enrolled nodes still on pre-hello
firmware we fall back to the edge/*/nodes.yaml manifest (chip=target, abilities=sensor) keyed by node_id —
so a standby node surfaces either way and the CLI (enroll/relocate) and server never drift.

Liveness is driven by the retained `status` flag, NOT by message recency: edge nodes publish status/hello
only on (re)connect, so an online-but-quiet node must not age out. The broker's LWT flips status to
"offline" when a node drops (or misses keepalive), which is what removes it from the candidate list.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("ha.ingest.edge_discovery")

REPO = Path(__file__).resolve().parents[2]
MANIFESTS = [REPO / "edge" / "esp32c6" / "nodes.yaml",
             REPO / "edge" / "esp32s3-eth" / "nodes.yaml"]
REGISTRY = REPO / "instance" / "devices.yaml"   # live placement truth (relocate updates it) — see load_registry_areas

HELLO_TOPIC = "home/edge/+/hello"
STATUS_TOPIC = "home/edge/+/status"

# An area value that means "not placed in a real room yet" → the node is available for intake.
UNASSIGNED_AREAS = {"", "standby", "unassigned", "none", "spare", "staging"}
# Drop rows we've seen go offline and not heard from again in this long (housekeeping only; online rows
# are never evicted on age).
OFFLINE_TTL_S = 24 * 3600

# manifest sensor -> the device_type ability string the firmware also emits (keep in lockstep with app_main)
_SENSOR_ABILITY = {"sgp30": "sgp30_gas", "sgp40": "sgp40_gas", "bme680": "bme680_gas"}


def load_manifest() -> dict:
    """node_id -> {mac,target,sensor,area,...} merged across the C6 + S3 manifests. Best-effort: a missing
    or unparseable manifest just yields fewer manifest-filled fields, never an error."""
    out: dict = {}
    try:
        import yaml
    except Exception:
        log.warning("edge_discovery: pyyaml unavailable — manifest fallback disabled")
        return out
    for p in MANIFESTS:
        try:
            raw = yaml.safe_load(p.read_text()) or {}
        except FileNotFoundError:
            continue
        except Exception:
            log.debug("edge_discovery: manifest %s unreadable", p, exc_info=True)
            continue
        for nid, rec in (raw.get("nodes") or {}).items():
            if isinstance(rec, dict):
                out[nid] = rec
    return out


def load_registry_areas() -> dict:
    """node_id -> assigned area from the LIVE device registry (instance/devices.yaml). This is the source of
    truth for placement (relocate updates it) and, unlike the build-time manifest, it can't drift on an
    air-gapped box — so it's the PRIMARY 'is this node already placed?' signal. A node with a gas device in a
    real room here is excluded from intake, which prevents a stale manifest from surfacing a live sensor as
    'adoptable' (and thus a mis-click relocating it)."""
    out: dict = {}
    try:
        import yaml
        reg = (yaml.safe_load(REGISTRY.read_text()) or {}).get("devices", {})
    except Exception:
        log.debug("edge_discovery: device registry unreadable — placement cross-check disabled", exc_info=True)
        return out
    for info in reg.values():
        nid, area = (info or {}).get("node_id"), (info or {}).get("area")
        if nid and area and nid not in out:
            out[nid] = area
    return out


def _topic_node(topic: str) -> str | None:
    """home/edge/<node>/{hello,status} -> <node> (or None if the topic isn't shaped that way)."""
    parts = topic.split("/")
    if len(parts) >= 4 and parts[0] == "home" and parts[1] == "edge":
        return parts[2]
    return None


class EdgeDiscoveryCache:
    """Thread-safe rolling cache of edge nodes keyed by node_id. Written by the MQTT subscriber thread
    (ingest_*), read by API request handlers (candidates) — hence the lock."""

    def __init__(self, offline_ttl_s: int = OFFLINE_TTL_S):
        self._offline_ttl = offline_ttl_s
        self._by_node: dict[str, dict] = {}
        self._lock = threading.Lock()

    def ingest_hello(self, node_id: str, payload: dict, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if not node_id or not isinstance(payload, dict):
            return
        with self._lock:
            row = self._by_node.setdefault(node_id, {"node": node_id, "first_seen": now})
            row["last_seen"] = now
            row["source"] = "hello"
            row["online"] = True            # a hello means the node just (re)connected
            for k in ("chip", "mac", "fw", "slot"):
                v = payload.get(k)
                if v is not None:
                    row[k] = v
            ab = payload.get("abilities")
            if isinstance(ab, list):
                row["abilities"] = [str(a) for a in ab]
            if "enrolled" in payload:
                row["enrolled"] = bool(payload["enrolled"])

    def ingest_status(self, node_id: str, raw: str, *, now: float | None = None) -> None:
        """`raw` is the retained status payload: 'online <slot> <fw>' or 'offline'."""
        now = time.time() if now is None else now
        if not node_id:
            return
        s = (raw or "").strip()
        online = s.startswith("online")
        with self._lock:
            row = self._by_node.setdefault(node_id, {"node": node_id, "first_seen": now})
            row["last_seen"] = now
            row["online"] = online
            parts = s.split()
            if online and len(parts) >= 3:     # 'online <slot> <fw>' — fill identity even without a hello
                row.setdefault("slot", parts[1])
                row.setdefault("fw", parts[2])

    def row(self, node_id: str) -> dict | None:
        """The cached row for one node, or None. A COPY — callers must not mutate cache state.

        Unlike ``candidates()`` this applies no online/not-yet-placed filtering: ADR-0036 intake needs a
        node's self-described abilities to auto-register its device record, and must not depend on the
        node still passing the candidate filter at that instant."""
        with self._lock:
            row = self._by_node.get(node_id)
            return dict(row) if row else None

    def candidates(self, *, manifest: dict | None = None, registry_areas: dict | None = None,
                   now: float | None = None) -> list[dict]:
        """Online, not-yet-placed edge nodes, annotated for the UI. Identity is merged from the manifest
        where `hello` didn't provide it. Newest-heard first."""
        now = time.time() if now is None else now
        manifest = load_manifest() if manifest is None else manifest
        registry_areas = load_registry_areas() if registry_areas is None else registry_areas
        out: list[dict] = []
        with self._lock:
            # housekeeping: drop long-offline rows so the map doesn't grow unbounded
            for nid in [n for n, r in self._by_node.items()
                        if not r.get("online") and now - r.get("last_seen", now) > self._offline_ttl]:
                del self._by_node[nid]
            rows = list(self._by_node.values())

        for row in rows:
            if not row.get("online"):
                continue
            node = row["node"]
            man = manifest.get(node, {})
            area = str(man.get("area") or "").strip().lower()
            reg_area = str(registry_areas.get(node, "")).strip().lower()
            # PLACED if EITHER the live registry OR the manifest puts it in a real room. The registry is the
            # primary signal (can't drift on the air-gapped box); the manifest is the fallback for nodes with
            # no device record yet. A node surfaces for intake only if NEITHER places it.
            placed = (reg_area and reg_area not in UNASSIGNED_AREAS) or \
                     (node in manifest and area and area not in UNASSIGNED_AREAS)
            if placed:
                continue                        # already placed in a real room — not an intake candidate
            # Only surface nodes we can actually intake: one bound to a device we can relocate (in the
            # registry), or one self-describing via hello (a fresh unit worth adopting). Without this a
            # placed-elsewhere relay with no gas device AND a stale/missing manifest entry would show up as
            # spurious "standby hardware" (harmless — intake 404s — but confusing).
            if node not in registry_areas and not row.get("abilities"):
                continue

            item = dict(row)
            # fill identity from the manifest for pre-hello firmware (hello values win — setdefault)
            if man.get("target"):
                item.setdefault("chip", man["target"])
            if man.get("mac"):
                item.setdefault("mac", man["mac"])
            if man.get("sensor") and "abilities" not in item:
                item["abilities"] = [_SENSOR_ABILITY.get(man["sensor"], f"{man['sensor']}_gas")]
            item.setdefault("abilities", [])
            item.setdefault("area", man.get("area") or "standby")
            item["known"] = node in manifest    # False ⇒ un-manifested hardware announcing itself
            item["age_s"] = round(now - row.get("last_seen", now), 1)
            out.append(item)

        out.sort(key=lambda r: r.get("last_seen", 0), reverse=True)
        return out


def start_subscriber(cache: EdgeDiscoveryCache, *, broker: str = "localhost", port: int = 1883):
    """Best-effort paho subscriber feeding `cache` from home/edge/+/{hello,status}. Returns the client
    (call loop_stop()/disconnect() on shutdown), or None if paho/broker is unavailable — in which case the
    edge-intake list simply stays empty and the rest of the API is unaffected (mirrors discovery.py)."""
    try:
        import paho.mqtt.client as mqtt
        from server.util.mqtt_creds import apply_credentials
    except Exception:
        log.warning("edge_discovery: paho unavailable — edge intake candidates will stay empty")
        return None

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    apply_credentials(client)

    def on_connect(c, userdata, flags, rc, properties=None):
        if rc == 0:
            c.subscribe([(HELLO_TOPIC, 0), (STATUS_TOPIC, 0)])
            log.info("edge_discovery subscribed to hello+status on %s:%s", broker, port)
        else:
            log.warning("edge_discovery MQTT connect failed rc=%s", rc)

    def on_message(c, userdata, msg):
        node = _topic_node(msg.topic)
        if not node:
            return
        try:
            if msg.topic.endswith("/hello"):
                cache.ingest_hello(node, json.loads(msg.payload.decode()))
            elif msg.topic.endswith("/status"):
                cache.ingest_status(node, msg.payload.decode())
        except Exception:
            log.debug("edge_discovery: undecodable payload on %s", msg.topic, exc_info=True)

    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(broker, port, keepalive=60)
    except Exception:
        log.warning("edge_discovery: broker %s:%s unreachable — edge discovery disabled this cycle",
                    broker, port, exc_info=True)
        return None
    client.loop_start()     # background thread handles reconnection
    return client
