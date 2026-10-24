"""In-process stand-in for the HTTP engine, for fast offline testing.

Wraps the stock (unmodified) engine.sim.Simulation behind the same method
names as solutions.client.Client, so the planner's run loop can be exercised
thousands of times without a server. The real demo always runs over HTTP.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from engine.sim import Simulation, SimulationError
from solutions.client import ClientError

ROOT = Path(__file__).resolve().parent.parent


def load_scenario(name_or_path: str) -> dict:
    p = Path(name_or_path)
    if not p.suffix:
        p = ROOT / "scenarios" / f"{name_or_path}.json"
    with open(p, encoding="utf-8") as f:
        return json.load(f)


class LocalClient:
    def __init__(self, scenario: dict):
        self.scenario = scenario
        self.sim = Simulation(scenario)

    def _call(self, fn, *args):
        try:
            out = fn(*args)
        except SimulationError as e:
            raise ClientError(str(e)) from None
        return copy.deepcopy(out)

    def health(self) -> dict:
        return {"ok": True, "clock": self.sim.clock, "scenario": self.sim.scenario_id}

    def state(self) -> dict:
        st = self.sim.state()
        log = st.pop("log")
        st = copy.deepcopy(st)
        st["log"] = list(log)
        return st

    def report(self) -> dict:
        return self._call(self.sim.report)

    def dispatch(self, vehicle_id: str, incident_id: str) -> dict:
        return self._call(self.sim.dispatch, vehicle_id, incident_id)

    def move(self, vehicle_id: str, location_id: str) -> dict:
        return self._call(self.sim.move, vehicle_id, location_id)

    def cancel(self, vehicle_id: str) -> dict:
        return self._call(self.sim.cancel, vehicle_id)

    def advance(self, to_hour: int | None = None) -> dict:
        return self._call(self.sim.advance, to_hour)

    def reset(self, scenario: str | None = None) -> dict:
        self.sim = Simulation(self.scenario)
        return {"ok": True, "clock": 0}
