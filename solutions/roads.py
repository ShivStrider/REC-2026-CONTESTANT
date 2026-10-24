"""Travel-time maths for the planner.

Mirrors the engine's rules exactly (docs/TEAM_GUIDE.md section 6) so our
feasibility checks agree with the arrival hour the engine will assign:

    arrival = clock + ceil(sum over route of hours * (1.5 if restricted) / speed)

Closed roads are removed from the graph. Roads are undirected. Each segment
is divided by speed before summing, in the same order as the engine, so
floating-point rounding matches too.
"""
from __future__ import annotations

import heapq
import math
from functools import lru_cache

RESTRICTED_FACTOR = 1.5


def graph_key(roads: list[dict]) -> tuple:
    """Hashable snapshot of the road network (changes when a status flips)."""
    return tuple(sorted(
        (r["id"], r["from_location_id"], r["to_location_id"],
         float(r["hours"]), r.get("status", "open"))
        for r in roads))


@lru_cache(maxsize=4096)
def _hours_from(key: tuple, start: str, speed: float) -> dict[str, float]:
    adj: dict[str, list[tuple[str, float]]] = {}
    for _, a, b, hours, status in key:
        if status == "closed":
            continue
        w = hours * (RESTRICTED_FACTOR if status == "restricted" else 1.0) / speed
        adj.setdefault(a, []).append((b, w))
        adj.setdefault(b, []).append((a, w))
    dist = {start: 0.0}
    heap = [(0.0, start)]
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist.get(u, math.inf):
            continue
        for v, w in adj.get(u, ()):
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                heapq.heappush(heap, (nd, v))
    return dist


class RoadMap:
    """Shortest-path travel hours over the current road network."""

    def __init__(self, roads: list[dict], locations: list[dict]):
        self.key = graph_key(roads)
        self.location_ids = [l["id"] for l in locations]

    def hours(self, start: str, goal: str, speed: float) -> int | None:
        """Whole hours the engine would charge for start -> goal, or None if unreachable."""
        if start == goal:
            return 0
        d = _hours_from(self.key, start, float(speed)).get(goal)
        return None if d is None else math.ceil(d)

    def round_trip(self, home: str, speed: float) -> tuple[int, str] | None:
        """Fastest out-and-back from `home` (needed to re-visit a location a
        vehicle is already parked at). Returns (total hours, turnaround location)."""
        best = None
        for loc in self.location_ids:
            if loc == home:
                continue
            out = self.hours(home, loc, speed)
            back = self.hours(loc, home, speed)
            if out is None or back is None:
                continue
            if best is None or out + back < best[0]:
                best = (out + back, loc)
        return best
