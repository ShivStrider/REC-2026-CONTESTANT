"""Dispatch strategy: turns one /state snapshot into a list of actions.

  basic  Earliest-deadline-first. For each open incident, send the available
         vehicle that arrives soonest -- but only if it arrives by the
         deadline (a late vehicle saves nobody and is stuck until it arrives).

The planner never sees the future: it only uses the snapshot it is given.
"""
from __future__ import annotations

from dataclasses import dataclass

from .roads import RoadMap

PROFILES = ("basic",)


@dataclass
class Action:
    kind: str                # "dispatch" | "move" | "cancel"
    vehicle_id: str
    target: str | None       # incident id (dispatch) or location id (move)
    reason: str
    tag: str = ""            # what the action is for: incident / ...


# --------------------------------------------------------------------------
# world view
# --------------------------------------------------------------------------

class World:
    """Read-only helpers over one /state snapshot."""

    def __init__(self, state: dict):
        self.state = state
        self.clock: int = state["clock"]
        self.horizon: int = state["horizon_hours"]
        self.map = RoadMap(state["roads"], state["locations"])
        self.loc_names = {l["id"]: l["name"] for l in state["locations"]}
        self.vehicles = {v["id"]: v for v in state["vehicles"]}
        self.incidents = {i["id"]: i for i in state["incidents"]}

    def open_unassigned(self) -> list[dict]:
        return [i for i in self.incidents.values()
                if i["status"] == "open" and i["assigned_vehicle_id"] is None]

    def available(self) -> list[dict]:
        return [v for v in self.vehicles.values() if v["status"] == "available"]

    def hours(self, start: str, goal: str, speed: float) -> int | None:
        return self.map.hours(start, goal, speed)

    def name(self, loc: str) -> str:
        return f"{loc} {self.loc_names.get(loc, '')}".strip()


# --------------------------------------------------------------------------
# planner
# --------------------------------------------------------------------------

class Planner:
    def __init__(self, profile: str = "basic", log=None):
        if profile not in PROFILES:
            raise ValueError(f"unknown profile {profile!r} (choose from {PROFILES})")
        self.profile = profile
        self.log = log
        self.wakeup: int | None = None          # hour we must be consulted at
        self.noted: set[tuple] = set()          # one-off notes already logged

    # -- bookkeeping ---------------------------------------------------------

    def _note(self, key: tuple, hour: int, kind: str, text: str) -> None:
        """Log an observation once (not every round)."""
        if key in self.noted:
            return
        self.noted.add(key)
        if self.log:
            self.log.decision(hour, kind, None, None, text)

    def on_applied(self, action: Action, result: dict) -> None:
        pass

    def on_rejected(self, action: Action) -> None:
        pass

    # -- entry point ---------------------------------------------------------

    def decide(self, state: dict) -> list[Action]:
        world = World(state)
        self.wakeup = None
        return self._greedy_incidents(world, used=set())

    # -- profile: basic ------------------------------------------------------

    def _greedy_incidents(self, world: World, used: set[str]) -> list[Action]:
        """Earliest deadline first; fastest feasible vehicle."""
        actions = []
        for inc in sorted(world.open_unassigned(),
                          key=lambda i: (i["deadline"], -i["people_at_risk"])):
            options = []
            for v in world.available():
                if v["id"] in used:
                    continue
                h = world.hours(v["location_id"], inc["location_id"], v["speed"])
                if h is None:
                    continue
                arrive = world.clock + h
                if arrive <= inc["deadline"]:
                    # earliest arrival; on a tie keep the faster vehicle free
                    options.append((arrive, v["speed"], v["id"]))
            if not options:
                self._note(("wait", inc["id"]), world.clock, "WAIT",
                           f"{inc['id']} ({inc['people_at_risk']} people, deadline "
                           f"H{inc['deadline']}): no available vehicle can arrive in time")
                continue
            arrive, _, vid = min(options)
            used.add(vid)
            alts = ", ".join(f"{o[2]}@H{o[0]}" for o in sorted(options) if o[2] != vid) or "none"
            actions.append(Action(
                "dispatch", vid, inc["id"],
                f"EDF: {inc['people_at_risk']} people at {world.name(inc['location_id'])}, "
                f"arrive H{arrive} <= deadline H{inc['deadline']} "
                f"(slack {inc['deadline'] - arrive}h); alternatives: {alts}",
                tag="incident"))
        return actions
