"""Tests for the planner's behaviour, run in-process against the stock engine.

tools.local_engine.LocalClient wraps the unmodified engine.sim.Simulation
behind the same methods as the HTTP client, so no server is needed.

    python -m unittest discover tests
"""
from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path

from engine.sim import Simulation
from solutions.decision_log import DecisionLog
from solutions.planner import run
from solutions.strategy import PROFILES, Action, Planner, World
from tools.local_engine import LocalClient, load_scenario
from tools.make_stress import build as build_stress

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_7DAY = {"basic": 88, "care": 88, "full": 108}
STRESS_SEEDS = (1, 2, 3, 4, 5)


class CheckedClient(LocalClient):
    """LocalClient that records every dispatch and any rule the planner breaks."""

    def __init__(self, scenario: dict):
        super().__init__(scenario)
        self.dispatches: list[dict] = []
        self.violations: list[str] = []

    def dispatch(self, vehicle_id: str, incident_id: str) -> dict:
        inc = self.sim.incidents.get(incident_id)
        if inc is not None and inc["assigned_vehicle_id"] is not None:
            self.violations.append(
                f"H{self.sim.clock}: {vehicle_id} sent to {incident_id}, "
                f"which already has {inc['assigned_vehicle_id']}")
        res = super().dispatch(vehicle_id, incident_id)
        self.dispatches.append(res)
        if not res["arrives_before_deadline"]:
            self.violations.append(
                f"H{self.sim.clock}: {vehicle_id} -> {incident_id} arrives "
                f"H{res['arrival_hour']} after deadline H{res['deadline_hour']}")
        return res


def play(scenario: dict, profile: str, planner: Planner | None = None,
         client: LocalClient | None = None) -> tuple[dict, DecisionLog, LocalClient]:
    """Run one scenario to the horizon; returns (report, log, client)."""
    log = DecisionLog(None, quiet=True)
    client = client or CheckedClient(scenario)
    planner = planner or Planner(profile, log)
    planner.log = log
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        report = run(client, planner, log, label=profile)
    return report, log, client


class SevenDay(unittest.TestCase):
    """Every profile finishes the training scenario with the expected score."""

    @classmethod
    def setUpClass(cls):
        cls.scenario = load_scenario("7day")
        cls.runs = {p: play(cls.scenario, p) for p in PROFILES}

    def test_profiles_complete_with_expected_scores(self):
        for profile in PROFILES:
            with self.subTest(profile=profile):
                report, _, _ = self.runs[profile]
                self.assertTrue(report["complete"])
                self.assertEqual(report["clock"], report["horizon_hours"])
                self.assertEqual(report["incidents_open"], 0)
                if profile in EXPECTED_7DAY:
                    self.assertEqual(report["people_saved_total"], EXPECTED_7DAY[profile])

    def test_care_profiles_lose_no_residents(self):
        for profile in ("care", "full"):
            with self.subTest(profile=profile):
                self.assertEqual(self.runs[profile][0]["people_lost_chronic"], 0)

    def test_never_dispatches_late_or_twice(self):
        for profile in PROFILES:
            with self.subTest(profile=profile):
                _, log, client = self.runs[profile]
                self.assertTrue(client.dispatches, "planner never dispatched")
                self.assertEqual(client.violations, [])
                self.assertEqual(log.counts.get("WARNING", 0), 0)

    def test_no_rejected_actions_or_errors(self):
        for profile in PROFILES:
            with self.subTest(profile=profile):
                _, log, _ = self.runs[profile]
                self.assertEqual(log.counts.get("REJECTED", 0), 0)
                self.assertEqual(log.counts.get("ERROR", 0), 0)

    def test_full_profile_uses_moves(self):
        # Requirement 4a: repositioning / care visits, not just dispatch.
        _, log, _ = self.runs["full"]
        moves = sum(log.counts.get(k, 0) for k in ("MOVE", "REPOSITION", "CARE-VISIT", "CARE-RELAY"))
        self.assertGreater(moves, 0)

    def test_rerun_after_reset_gives_same_result(self):
        # Requirement 5c: stopped and re-run cleanly against a reset engine.
        report1, _, client = play(self.scenario, "full")
        client.reset()
        report2, _, _ = play(self.scenario, "full", client=client)
        self.assertEqual(report1["people_saved_total"], report2["people_saved_total"])

    def test_refuses_engine_already_at_horizon(self):
        _, _, client = self.runs["basic"]
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(run(client, Planner("basic"), DecisionLog(None, quiet=True)))


class Robustness(unittest.TestCase):
    """Rejected actions and strategy crashes must not end the run."""

    def test_rejected_action_is_logged_and_run_continues(self):
        class BadFirstRound(Planner):
            sent_bad = False

            def decide(self, state):
                actions = super().decide(state)
                if not self.sent_bad:
                    self.sent_bad = True
                    actions.insert(0, Action("dispatch", "VEH-NOPE", "INC-001",
                                             "test: unknown vehicle", tag="incident"))
                return actions

        report, log, _ = play(load_scenario("7day"), "full", planner=BadFirstRound("full"))
        self.assertEqual(log.counts.get("REJECTED", 0), 1)
        self.assertTrue(report["complete"])
        self.assertEqual(report["people_saved_total"], EXPECTED_7DAY["full"])

    def test_strategy_crash_falls_back_and_run_completes(self):
        class CrashesOnce(Planner):
            crashed = False

            def decide(self, state):
                if state["clock"] >= 40 and not self.crashed:
                    self.crashed = True
                    raise RuntimeError("test crash")
                return super().decide(state)

        report, log, _ = play(load_scenario("7day"), "full", planner=CrashesOnce("full"))
        self.assertEqual(log.counts.get("ERROR", 0), 1)
        self.assertTrue(report["complete"])

    def test_breakdown_mid_mission_is_recovered(self):
        # One vehicle, one incident 4h away. The vehicle breaks down 2h into
        # the trip (force-cancelled back to LOC-1) and returns at hour 3; it
        # can still arrive by H8 <= deadline H14, so the incident must be saved.
        sc = load_scenario("7day")
        scenario = {
            **sc, "chronic": None,
            "vehicles": [{"id": "VEH-A", "name": "A", "speed": 1.25, "location_id": "LOC-1"}],
            "events": [
                {"time": 0, "type": "incident", "data": {
                    "id": "INC-T", "title": "t", "location_id": "LOC-6",
                    "people_at_risk": 5, "deadline_offset_hours": 14}},
                {"time": 2, "type": "vehicle_status",
                 "data": {"vehicle_id": "VEH-A", "status": "out_of_service"}},
                {"time": 3, "type": "vehicle_status",
                 "data": {"vehicle_id": "VEH-A", "status": "available"}},
            ]}
        for profile in ("care", "full"):
            with self.subTest(profile=profile):
                report, log, client = play(scenario, profile)
                self.assertEqual(report["people_saved"], 5)
                self.assertEqual(log.counts.get("RECOVER", 0), 1)
                self.assertEqual(client.violations, [])

    def test_unreachable_incident_is_not_dispatched(self):
        # Deadline 1h at a place 4h+ away from every vehicle: nobody can make
        # it, so the planner must not waste a vehicle on it.
        sc = load_scenario("7day")
        scenario = {**sc, "events": [
            {"time": 0, "type": "incident", "data": {
                "id": "INC-FAR", "title": "far", "location_id": "LOC-6",
                "people_at_risk": 9, "deadline_offset_hours": 1}}]}
        for profile in PROFILES:
            with self.subTest(profile=profile):
                _, _, client = play(scenario, profile)
                self.assertNotIn("INC-FAR", [d["incident_id"] for d in client.dispatches])


class CareWindow(unittest.TestCase):
    """A visit arriving in [tick-24, tick] covers the tick, both ends inclusive,
    so one arrival exactly on a tick hour covers two ticks."""

    def state(self, last_visit: int, next_tick: int, clock: int) -> dict:
        sc = load_scenario("7day")
        sim = Simulation({**sc, "events": []})
        st = sim.state()
        st["clock"] = clock
        st["chronic"] = {**st["chronic"], "last_visit": last_visit, "next_tick": next_tick}
        return st

    def test_visit_on_tick_covers_next_tick(self):
        need = World(self.state(last_visit=24, next_tick=48, clock=30)).care_need()
        self.assertEqual(need.tick, 72)
        self.assertEqual(need.window_start, 48)

    def test_visit_one_hour_early_does_not(self):
        need = World(self.state(last_visit=23, next_tick=48, clock=30)).care_need()
        self.assertEqual(need.tick, 48)

    def test_scheduled_arrival_counts(self):
        need = World(self.state(last_visit=24, next_tick=48, clock=30)).care_need(
            extra_visits=(72,))
        self.assertEqual(need.tick, 120)

    def test_engine_agrees(self):
        # The engine itself: arrivals at H24 and H72 keep ticks 24..96 safe.
        scenario = {"horizon_hours": 120,
                    "locations": [{"id": "A"}, {"id": "C"}],
                    "roads": [{"id": "R", "from_location_id": "A", "to_location_id": "C",
                               "hours": 8, "status": "open"}],
                    "vehicles": [{"id": "V", "speed": 1.0, "location_id": "A"}],
                    "chronic": {"location_id": "C", "interval_hours": 24,
                                "loss_per_tick": 2, "initial_people": 24}}
        sim = Simulation(scenario)
        sim.advance(16), sim.move("V", "C")      # arrives H24
        sim.advance(40), sim.move("V", "A")
        sim.advance(64), sim.move("V", "C")      # arrives H72
        sim.advance(96)
        self.assertEqual(sim.people_lost_chronic, 0)
        sim.advance(120)
        self.assertEqual(sim.people_lost_chronic, 2)


class Stress(unittest.TestCase):
    """Synthetic 90-day scenarios (tools/make_stress.py): every profile reaches
    the horizon without a rejected action, a late dispatch or a double dispatch."""

    def test_stress_scenarios(self):
        for seed in STRESS_SEEDS:
            scenario = build_stress(seed)
            for profile in PROFILES:
                with self.subTest(seed=seed, profile=profile):
                    report, log, client = play(scenario, profile)
                    self.assertTrue(report["complete"])
                    self.assertEqual(log.counts.get("REJECTED", 0), 0)
                    self.assertEqual(log.counts.get("ERROR", 0), 0)
                    self.assertEqual(client.violations, [])

    def test_full_beats_basic(self):
        for seed in STRESS_SEEDS[:2]:
            with self.subTest(seed=seed):
                scenario = build_stress(seed)
                basic = play(scenario, "basic")[0]["people_saved_total"]
                full = play(scenario, "full")[0]["people_saved_total"]
                self.assertGreater(full, basic)


@unittest.skipUnless((ROOT / "scenarios" / "90day.json").is_file(), "90day.json not released yet")
class NinetyDay(unittest.TestCase):
    def test_every_profile_completes_cleanly(self):
        scenario = load_scenario("90day")
        for profile in PROFILES:
            with self.subTest(profile=profile):
                report, log, client = play(scenario, profile)
                self.assertTrue(report["complete"])
                self.assertEqual(log.counts.get("REJECTED", 0), 0)
                self.assertEqual(client.violations, [])


if __name__ == "__main__":
    unittest.main()
