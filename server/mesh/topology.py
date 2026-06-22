"""Pure mesh-topology graph + pathfinder — no I/O, fully unit-testable.

A node in the graph is a (kind, id) pair:
    ('server', 'server')          the dictator (.245), origin of every pull
    ('node',   'c6-bench')        an edge node (ESP32-C6)
    ('endpoint', 'meter_pro_...')  a leaf sensor (keyed by device_id)

A Link is a directed, observed edge between two graph nodes:
    Link(src, dst, kind, rssi, n_ok, n_fail, age_s)
  kind ∈ {'ip', 'espnow', 'ble-adv', 'ble-gatt'}
    ip       server↔node or node↔node backhaul (IP/MQTT reachable). No rssi.
    espnow   node↔node radio (future multi-node mesh). Has rssi.
    ble-adv  a receiver HEARS an endpoint's advertisements (necessary for a pull). Has rssi.
    ble-gatt a receiver has CONNECTED to an endpoint (stronger than adv).

best_path() runs Dijkstra from ('server','server') to an endpoint, minimising summed link cost.
The returned path is the hop chain a backfill must traverse; its length tells the dispatcher whether
it's executable today (≤1 relay hop) or needs the not-yet-built multi-hop relay transport.

Reachability (can a receiver HEAR the endpoint) and pull-capability (has it ever successfully PULLED
it) are different facts: A8:02 is the loudest meter to the host yet the host has never pulled it. So
cost folds BOTH — link rssi/reliability for every hop, plus a terminal pull-history adjustment passed
in via `pull_stats` so the chooser prefers a node that has actually succeeded.
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

SERVER = ("server", "server")

# link base costs (lower = preferred). IP backhaul is cheap+reliable; BLE depends on rssi.
_BASE = {"ip": 1.0, "ble-gatt": 2.0, "espnow": 3.0, "ble-adv": 4.0}
_RSSI_REF = -50          # dBm at/above which a radio link adds ~no penalty
_RSSI_SCALE = 10.0       # each 10 dBm weaker adds ~1.0 cost
_FAIL_WEIGHT = 8.0       # a link that keeps failing gets pushed toward last-resort
_STALE_S = 3600          # links older than this are treated as unreliable (decayed)
PULL_FAIL_PENALTY = 50.0  # terminal node has only-failed to pull this endpoint → avoid if alternative
PULL_OK_BONUS = 6.0       # terminal node has succeeded → strongly prefer
UNREACHABLE = math.inf


@dataclass(frozen=True)
class Link:
    src: tuple              # (kind, id)
    dst: tuple              # (kind, id)
    kind: str               # 'ip' | 'espnow' | 'ble-adv' | 'ble-gatt'
    rssi: int | None = None
    n_ok: int = 0
    n_fail: int = 0
    age_s: float = 0.0


def link_cost(link: Link) -> float:
    """Cost of traversing one observed link. inf if it's effectively dead."""
    base = _BASE.get(link.kind, 5.0)
    if link.kind in ("ble-adv", "ble-gatt", "espnow") and link.rssi is not None:
        base += max(0.0, (_RSSI_REF - link.rssi) / _RSSI_SCALE)
    total = link.n_ok + link.n_fail
    if total:
        base += _FAIL_WEIGHT * (link.n_fail / total)
    if link.age_s > _STALE_S:                     # decay stale observations
        base += math.log2(1 + link.age_s / _STALE_S)
    return base


def _terminal_adjust(node: tuple, endpoint: tuple, pull_stats: dict | None) -> float:
    """Bonus/penalty applied to the FINAL receiver→endpoint hop from its pull history with that
    endpoint. pull_stats maps (receiver_id, device_id) -> (n_ok, n_fail)."""
    if not pull_stats:
        return 0.0
    ok, fail = pull_stats.get((node[1], endpoint[1]), (0, 0))
    if ok:
        return -PULL_OK_BONUS
    if fail:
        return PULL_FAIL_PENALTY
    return 0.0


@dataclass
class Graph:
    adj: dict = field(default_factory=dict)   # src -> list[Link]
    nodes: set = field(default_factory=set)

    def add(self, link: Link) -> None:
        self.adj.setdefault(link.src, []).append(link)
        self.nodes.add(link.src)
        self.nodes.add(link.dst)


def build_graph(links) -> Graph:
    g = Graph()
    for l in links:
        g.add(l)
    return g


def best_path(graph: Graph, endpoint: tuple, src: tuple = SERVER, pull_stats: dict | None = None):
    """Dijkstra from src to endpoint. Returns (path, cost) where path is [src, ..., endpoint], or
    (None, inf) if unreachable. The terminal pull-history adjustment makes a proven puller win even
    if a louder receiver exists."""
    dist = {src: 0.0}
    prev: dict = {}
    pq = [(0.0, src)]
    seen = set()
    while pq:
        d, u = heapq.heappop(pq)
        if u in seen:
            continue
        seen.add(u)
        if u == endpoint:
            break
        for link in graph.adj.get(u, []):
            c = link_cost(link)
            if link.dst == endpoint:
                c += _terminal_adjust(u, endpoint, pull_stats)
            c = max(c, 0.0)
            nd = d + c
            if nd < dist.get(link.dst, UNREACHABLE):
                dist[link.dst] = nd
                prev[link.dst] = u
                heapq.heappush(pq, (nd, link.dst))
    if endpoint not in dist:
        return None, UNREACHABLE
    # reconstruct
    path = [endpoint]
    while path[-1] != src:
        path.append(prev[path[-1]])
    path.reverse()
    return path, dist[endpoint]


def hops(path) -> int:
    """Number of relay hops between server and endpoint (0 = server pulls directly via its own radio,
    1 = one edge node relays, ≥2 = needs the multi-hop relay transport)."""
    return max(0, len(path) - 2)


def serialize(path) -> str:
    return ">".join(f"{k}:{i}" if k != "endpoint" else i for k, i in path) if path else ""
