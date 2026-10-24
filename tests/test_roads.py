"""Tests for solutions/roads.py: our travel maths must match the engine's.

The engine is the reference: engine.travel.shortest_hours for route hours and
engine.sim.Simulation for the arrival hour it actually assigns. If RoadMap
ever disagrees, the planner's feasibility check is wrong.

    python -m unittest discover tests
"""
from __future__ import annotations

import itertools
import json
import math
import unittest
from pathlib import Path

from engine import travel
from engine.sim import Simulation
from solutions.roads import RoadMap

ROOT = Path(__file__).resolve().parent.parent
SPEEDS = (1.0, 1.25, 1.2, 0.8)
STATUSES = ("open", "restricted", "closed")


def load(name: str) -> dict:
    with open(ROOT / "scenarios" / f"{name}.json", encoding="utf-8") as f:
        return json.load(f)


def with_status(roads: list[dict], statuses: dict[str, str]) -> list[dict]:
    """Copy of the road list with some statuses flipped."""
    return [{**r, "status": statuses.get(r["id"], r["status"])} for r in roads]


def engine_hours(roads: list[dict], start: str, goal: str, speed: float) -> int | None:
    """Whole hours the engine charges, using the engine's own code."""
    h = travel.shortest_hours({r["id"]: r for r in roads}, start, goal, speed)
    return None if h is None else math.ceil(h)


class RoadMapMatchesEngine(unittest.TestCase):
    """RoadMap.hours == ceil(engine.travel.shortest_hours) everywhere."""

    def setUp(self):
        self.scenario = load("7day")
        self.roads = self.scenario["roads"]
        self.locations = self.scenario["locations"]
        self.loc_ids = [l["id"] for l in self.locations]

    def assert_all_pairs_match(self, roads: list[dict], label: str) -> None:
        rm = RoadMap(roads, self.locations)
        for speed in SPEEDS:
            for a, b in itertools.product(self.loc_ids, repeat=2):
                self.assertEqual(rm.hours(a, b, speed), engine_hours(roads, a, b, speed),
                                 f"{label}: {a}->{b} at speed {speed}")

    def test_all_open(self):
        self.assert_all_pairs_match(with_status(self.roads, {}), "all open")

    def test_all_restricted(self):
        self.assert_all_pairs_match(
            with_status(self.roads, {r["id"]: "restricted" for r in self.roads}),
            "all restricted")

    def test_each_road_restricted_or_closed(self):
        for r in self.roads:
            for status in ("restricted", "closed"):
                self.assert_all_pairs_match(with_status(self.roads, {r["id"]: status}),
                                            f"{r['id']} {status}")

    def test_every_status_combination(self):
        # 3^8 = 6561 road networks for the 7-day map: every one must agree.
        ids = [r["id"] for r in self.roads]
        rm_speeds = (1.0, 1.25)
        for combo in itertools.product(STATUSES, repeat=len(ids)):
            roads = with_status(self.roads, dict(zip(ids, combo)))
            rm = RoadMap(roads, self.locations)
            for speed in rm_speeds:
                for a in self.loc_ids:
                    for b in self.loc_ids:
                        if rm.hours(a, b, speed) != engine_hours(roads, a, b, speed):
                            self.fail(f"{dict(zip(ids, combo))}: {a}->{b} at speed {speed}")

    def test_scripted_road_events(self):
        # Replay the 7-day road_status events in order; check after each one.
        statuses: dict[str, str] = {}
        for e in self.scenario["events"]:
            if e["type"] != "road_status":
                continue
            statuses[e["data"]["road_id"]] = e["data"]["status"]
            self.assert_all_pairs_match(with_status(self.roads, statuses),
                                        f"after event at H{e['time']}")

    def test_other_scenarios(self):
        # Same check for any other scenario in the kit (e.g. 90day once released).
        for path in sorted((ROOT / "scenarios").glob("*.json")):
            if path.stem == "7day":
                continue
            with self.subTest(scenario=path.stem):
                sc = load(path.stem)
                rm = RoadMap(sc["roads"], sc["locations"])
                ids = [l["id"] for l in sc["locations"]]
                for v in sc["vehicles"]:
                    for a, b in itertools.product(ids, repeat=2):
                        self.assertEqual(rm.hours(a, b, v["speed"]),
                                         engine_hours(sc["roads"], a, b, v["speed"]))


class RoadMapMatchesSimulation(unittest.TestCase):
    """The arrival hour the engine assigns to a real /move equals clock + RoadMap.hours."""

    def test_move_arrival_hours(self):
        scenario = load("7day")
        rm = RoadMap(scenario["roads"], scenario["locations"])
        for v in scenario["vehicles"]:
            for loc in scenario["locations"]:
                if loc["id"] == v["location_id"]:
                    continue
                sc = {**scenario, "events": [],
                      "vehicles": [{**v, "status": "available"}]}
                sim = Simulation(sc)
                res = sim.move(v["id"], loc["id"])
                self.assertEqual(res["arrival_hour"] - sim.clock,
                                 rm.hours(v["location_id"], loc["id"], v["speed"]),
                                 f"{v['id']} {v['location_id']}->{loc['id']}")

    def test_dispatch_arrival_with_restricted_road(self):
        # Restricted Ridge Highway: ambulance LOC-1 -> LOC-6 is 5 * 1.5 / 1.25 = 6h.
        scenario = load("7day")
        roads = with_status(scenario["roads"], {"RD-3": "restricted"})
        sc = {**scenario, "roads": roads, "events": [
            {"time": 0, "type": "incident",
             "data": {"id": "T-1", "title": "test", "location_id": "LOC-6",
                      "people_at_risk": 1, "deadline_offset_hours": 20}}]}
        sim = Simulation(sc)
        res = sim.dispatch("VEH-AMB", "T-1")
        rm = RoadMap(roads, scenario["locations"])
        self.assertEqual(rm.hours("LOC-1", "LOC-6", 1.25), 6)
        self.assertEqual(res["arrival_hour"], rm.hours("LOC-1", "LOC-6", 1.25))


class RoadMapBasics(unittest.TestCase):
    def setUp(self):
        sc = load("7day")
        self.roads = sc["roads"]
        self.locations = sc["locations"]
        self.loc_ids = [l["id"] for l in self.locations]
        self.rm = RoadMap(self.roads, self.locations)

    def test_symmetric(self):
        # Roads are undirected, so a -> b costs the same as b -> a.
        for speed in SPEEDS:
            for a, b in itertools.combinations(self.loc_ids, 2):
                self.assertEqual(self.rm.hours(a, b, speed), self.rm.hours(b, a, speed))

    def test_start_equals_goal_is_zero(self):
        for loc in self.loc_ids:
            self.assertEqual(self.rm.hours(loc, loc, 1.0), 0)

    def test_known_hours(self):
        self.assertEqual(self.rm.hours("LOC-1", "LOC-2", 1.0), 8)
        self.assertEqual(self.rm.hours("LOC-1", "LOC-2", 1.25), 7)    # ceil(6.4)
        self.assertEqual(self.rm.hours("LOC-1", "LOC-5", 1.0), 5)     # via LOC-3
        self.assertEqual(self.rm.hours("LOC-1", "LOC-5", 1.25), 4)

    def test_restricted_is_one_and_a_half(self):
        rm = RoadMap(with_status(self.roads, {"RD-1": "restricted"}), self.locations)
        # Main Street 8h -> 12h restricted; the detour via LOC-3/LOC-5 (2+3+8) is 13h.
        self.assertEqual(rm.hours("LOC-1", "LOC-2", 1.0), 12)

    def test_closed_road_takes_detour(self):
        rm = RoadMap(with_status(self.roads, {"RD-4": "closed"}), self.locations)
        # Mobile Lane closed: LOC-1 -> LOC-3 must go round via LOC-2 and LOC-5.
        self.assertEqual(rm.hours("LOC-1", "LOC-3", 1.0), 8 + 8 + 3)

    def test_closed_with_no_other_route_is_none(self):
        # Close every road into the care home: it becomes unreachable.
        cut = {"RD-1": "closed", "RD-6": "closed", "RD-7": "closed"}
        rm = RoadMap(with_status(self.roads, cut), self.locations)
        for loc in self.loc_ids:
            if loc != "LOC-2":
                self.assertIsNone(rm.hours(loc, "LOC-2", 1.0))
                self.assertIsNone(rm.hours("LOC-2", loc, 1.0))
        self.assertEqual(rm.hours("LOC-2", "LOC-2", 1.0), 0)

    def test_graph_key_changes_when_status_flips(self):
        rm2 = RoadMap(with_status(self.roads, {"RD-3": "restricted"}), self.locations)
        self.assertNotEqual(self.rm.key, rm2.key)


class RoundTrip(unittest.TestCase):
    """round_trip(home) = cheapest out-and-back, used for the care-home relay."""

    def setUp(self):
        sc = load("7day")
        self.roads = sc["roads"]
        self.locations = sc["locations"]
        self.loc_ids = [l["id"] for l in self.locations]

    def brute_force(self, rm: RoadMap, home: str, speed: float) -> int | None:
        totals = [rm.hours(home, loc, speed) + rm.hours(loc, home, speed)
                  for loc in self.loc_ids
                  if loc != home and rm.hours(home, loc, speed) is not None]
        return min(totals) if totals else None

    def test_care_home_round_trip(self):
        rm = RoadMap(self.roads, self.locations)
        # Every road into the care home (LOC-2) is 8h.
        total, turn = rm.round_trip("LOC-2", 1.0)
        self.assertEqual(total, 16)
        self.assertNotEqual(turn, "LOC-2")
        self.assertEqual(rm.round_trip("LOC-2", 1.25)[0], 14)          # 7 + 7

    def test_round_trip_is_cheapest_for_every_home(self):
        for status in ("open", "restricted"):
            roads = with_status(self.roads, {r["id"]: status for r in self.roads})
            rm = RoadMap(roads, self.locations)
            for speed in SPEEDS:
                for home in self.loc_ids:
                    total, turn = rm.round_trip(home, speed)
                    self.assertEqual(total, self.brute_force(rm, home, speed),
                                     f"{status} {home} speed {speed}")
                    self.assertEqual(total, rm.hours(home, turn, speed)
                                     + rm.hours(turn, home, speed))

    def test_round_trip_isolated_home_is_none(self):
        cut = {"RD-1": "closed", "RD-6": "closed", "RD-7": "closed"}
        rm = RoadMap(with_status(self.roads, cut), self.locations)
        self.assertIsNone(rm.round_trip("LOC-2", 1.0))


if __name__ == "__main__":
    unittest.main()
