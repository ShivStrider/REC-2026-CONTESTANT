"""Shortest-path travel times over the scenario road graph.

Roads are undirected segments with a travel time in hours:
- status "closed"     -> segment impassable
- status "restricted" -> passable at 1.5x travel time
- vehicle "speed"     divides travel time (1.2 = 20% faster)

Results are memoised per (graph, origin, speed) because rollout evaluations
recompute the same trips constantly.
"""
from __future__ import annotations

import heapq
from functools import lru_cache
from types import MappingProxyType

RESTRICTED_FACTOR = 1.5


def segment_hours(road: dict, speed: float = 1.0) -> float:
    hours = road["hours"]
    if road.get("status") == "restricted":
        hours *= RESTRICTED_FACTOR
    return hours / speed


def _graph_key(roads: dict) -> tuple:
    return tuple(sorted(
        (r["id"], r["from_location_id"], r["to_location_id"],
         float(r["hours"]), r.get("status", "open"))
        for r in roads.values()))


@lru_cache(maxsize=2048)
def _hours_from(key: tuple, start: str, speed: float) -> dict:
    adj: dict[str, list[tuple[str, float]]] = {}
    for _, a, b, hours, status in key:
        if status == "closed":
            continue
        w = hours * (RESTRICTED_FACTOR if status == "restricted" else 1.0) / speed
        adj.setdefault(a, []).append((b, w))
        adj.setdefault(b, []).append((a, w))
    dist = {start: 0.0}
    heap: list[tuple[float, str]] = [(0.0, start)]
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist.get(u, float("inf")):
            continue
        for v, w in adj.get(u, ()):
            nd = d + w
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                heapq.heappush(heap, (nd, v))
    return MappingProxyType(dist)


def hours_from(roads: dict, start: str, speed: float = 1.0) -> dict:
    """Mapping of every reachable location -> travel hours from start."""
    return _hours_from(_graph_key(roads), start, speed)


def shortest_hours(roads: dict, start: str, goal: str, speed: float = 1.0) -> float | None:
    """Travel hours from start to goal, or None if unreachable."""
    if start == goal:
        return 0.0
    return hours_from(roads, start, speed).get(goal)
