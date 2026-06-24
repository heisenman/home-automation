"""Preferred-source assignment for live adv dedup (ADR-0015 Phase A) — pure, unit-testable.

For each endpoint (device_id) the dictator assigns ONE preferred source (the dictator's own radio =
"local", or a specific edge node) from observed adv reach, so the mapper republishes a meter only from
that source and drops the rest. Two stabilisers, both required by the ADR:

  * Hysteresis (sticky): switch the assigned source only if a challenger beats the incumbent by
    >= SWITCH_MARGIN cost AND sustains that lead for >= SWITCH_DWELL_S. No flapping on RSSI noise —
    same spirit as the failover primary-supremacy debounce.
  * Stale-source failover: if the assigned source stops being heard for > FRESH_WINDOW_S, demote it and
    promote the next-best fresh source.

Scoring reuses server/mesh/topology so live-adv selection and backfill routing share one cost model:
local source  -> Link(SERVER, endpoint, 'ble-adv', rssi)
edge source N -> Link(SERVER, ('node',N), 'ip') + Link(('node',N), endpoint, 'ble-adv', rssi)
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from server.mesh.topology import SERVER, Link, best_relay, build_graph

LOCAL = "local"  # the dictator's own onboard radio, as a source id

FRESH_WINDOW_S = float(os.environ.get("HA_RELAY_FRESH_S", "120"))   # source silent longer -> failover
SWITCH_MARGIN = float(os.environ.get("HA_RELAY_SWITCH_MARGIN", "0.6"))  # ~6 dBm of cost (rssi_scale=10)
SWITCH_DWELL_S = float(os.environ.get("HA_RELAY_SWITCH_DWELL_S", "30"))


@dataclass
class _Obs:
    rssi: int | None
    ts: float


def _source_node(source_id: str) -> tuple:
    return SERVER if source_id == LOCAL else ("node", source_id)


def _source_id(node: tuple) -> str:
    return LOCAL if node == SERVER else node[1]


class Assigner:
    """Tracks per-device adv observations and yields a sticky preferred source."""

    def __init__(self, fresh_window_s: float = FRESH_WINDOW_S,
                 switch_margin: float = SWITCH_MARGIN, switch_dwell_s: float = SWITCH_DWELL_S):
        self.fresh_window_s = fresh_window_s
        self.switch_margin = switch_margin
        self.switch_dwell_s = switch_dwell_s
        self._obs: dict[str, dict[str, _Obs]] = {}          # device_id -> {source_id: _Obs}
        self._assigned: dict[str, str] = {}                 # device_id -> source_id
        self._challenge: dict[str, tuple[str, float]] = {}  # device_id -> (challenger_id, since_ts)

    def observe(self, device_id: str, source_id: str, rssi: int | None, ts: float) -> None:
        self._obs.setdefault(device_id, {})[source_id] = _Obs(rssi, ts)

    def _graph(self, device_id: str, now: float):
        """Graph of FRESH sources only (stale ones excluded == failover)."""
        endpoint = ("endpoint", device_id)
        links: list[Link] = []
        for sid, o in self._obs.get(device_id, {}).items():
            if now - o.ts > self.fresh_window_s:
                continue
            if sid == LOCAL:
                links.append(Link(SERVER, endpoint, "ble-adv", rssi=o.rssi))
            else:
                links.append(Link(SERVER, ("node", sid), "ip"))
                links.append(Link(("node", sid), endpoint, "ble-adv", rssi=o.rssi))
        return build_graph(links), endpoint

    def _cost_of(self, device_id: str, source_id: str, now: float) -> float:
        """Cost via a single specific source (for the incumbent's current standing)."""
        o = self._obs.get(device_id, {}).get(source_id)
        if not o or now - o.ts > self.fresh_window_s:
            return float("inf")
        endpoint = ("endpoint", device_id)
        if source_id == LOCAL:
            links = [Link(SERVER, endpoint, "ble-adv", rssi=o.rssi)]
        else:
            links = [Link(SERVER, ("node", source_id), "ip"),
                     Link(("node", source_id), endpoint, "ble-adv", rssi=o.rssi)]
        _, _, cost = best_relay(build_graph(links), endpoint)
        return cost

    def preferred(self, device_id: str, now: float) -> str | None:
        """The sticky preferred source id for this device, or None if nothing fresh is heard it."""
        graph, endpoint = self._graph(device_id, now)
        node, hops, best_cost = best_relay(graph, endpoint)
        if node is None:
            self._assigned.pop(device_id, None)
            self._challenge.pop(device_id, None)
            return None
        best_id = _source_id(node)
        incumbent = self._assigned.get(device_id)

        # no incumbent, or incumbent went stale/unreachable -> take the best now (failover/cold start)
        if incumbent is None or self._cost_of(device_id, incumbent, now) == float("inf"):
            self._assigned[device_id] = best_id
            self._challenge.pop(device_id, None)
            return best_id
        if best_id == incumbent:
            self._challenge.pop(device_id, None)
            return incumbent

        # a different source is best — only switch if it BEATS the incumbent by >= margin, sustained.
        inc_cost = self._cost_of(device_id, incumbent, now)
        if inc_cost - best_cost < self.switch_margin:
            self._challenge.pop(device_id, None)
            return incumbent
        chal, since = self._challenge.get(device_id, (None, now))
        if chal != best_id:
            self._challenge[device_id] = (best_id, now)
            return incumbent
        if now - since >= self.switch_dwell_s:
            self._assigned[device_id] = best_id
            self._challenge.pop(device_id, None)
            return best_id
        return incumbent
