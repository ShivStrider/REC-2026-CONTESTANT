"""Dispatch strategy: turns one /state snapshot into a list of actions.

Two profiles, each building on the previous one, so their scores can be
compared like-for-like:

  basic  Earliest-deadline-first. For each open incident, send the available
         vehicle that arrives soonest -- but only if it arrives by the
         deadline (a late vehicle saves nobody and is stuck until it arrives).

  care   basic + care-home scheduling + breakdown recovery.
         The home loses residents at every tick hour T unless some vehicle
         arrived during [T - interval, T] (both ends inclusive in the engine).
         We send one vehicle to arrive exactly AT the tick hour: that single
         visit covers tick T and tick T + interval, halving care trips.
         A vehicle parked at the home does the out-and-back relay.
         An incident may take the care vehicle only if it saves more people
         than the residents that tick would cost.

The planner never sees the future: it only uses the snapshot it is given.
"""
from __future__ import annotations

from dataclasses import dataclass

from .roads import RoadMap

PROFILES = ("basic", "care")


@dataclass
class Action:
    kind: str                # "dispatch" | "move" | "cancel"
    vehicle_id: str
    target: str | None       # incident id (dispatch) or location id (move)
    reason: str
    tag: str = ""            # what the action is for: incident / care / relay / reposition / cancel


@dataclass
class CareNeed:
    tick: int                # earliest tick hour not yet covered by a visit
    window_start: int        # a visit arriving in [window_start, tick] covers it
    value: int               # residents lost if the tick is missed


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
        self.chronic = state.get("chronic")
        self.care_loc = self.chronic["location_id"] if self.chronic else None

    def open_unassigned(self) -> list[dict]:
        return [i for i in self.incidents.values()
                if i["status"] == "open" and i["assigned_vehicle_id"] is None]

    def available(self) -> list[dict]:
        return [v for v in self.vehicles.values() if v["status"] == "available"]

    def hours(self, start: str, goal: str, speed: float) -> int | None:
        return self.map.hours(start, goal, speed)

    def care_hours(self, start: str, speed: float) -> tuple[int, str | None] | None:
        """Hours for a vehicle at `start` to *arrive* at the care home.
        From the home itself that is an out-and-back trip (relay)."""
        if start == self.care_loc:
            rt = self.map.round_trip(start, speed)
            return None if rt is None else (rt[0], rt[1])
        h = self.hours(start, self.care_loc, speed)
        return None if h is None else (h, None)

    def name(self, loc: str) -> str:
        return f"{loc} {self.loc_names.get(loc, '')}".strip()

    def care_need(self, exclude_vehicle: str | None = None,
                  extra_visits: tuple[int, ...] = ()) -> CareNeed | None:
        """Earliest care tick that no past or already-scheduled visit covers."""
        c = self.chronic
        if not c or c["people_remaining"] <= 0:
            return None
        interval = c["interval_hours"]
        visits = [c["last_visit"], *extra_visits]
        for v in self.vehicles.values():
            m = v.get("mission")
            if m and m["target"] == self.care_loc and v["id"] != exclude_vehicle:
                visits.append(m["arrival"])
        tick = c["next_tick"]
        while tick <= self.horizon:
            if not any(tick - interval <= t <= tick for t in visits):
                return CareNeed(tick, tick - interval,
                                min(c["loss_per_tick"], c["people_remaining"]))
            tick += interval
        return None


# --------------------------------------------------------------------------
# planner
# --------------------------------------------------------------------------

class Planner:
    def __init__(self, profile: str = "care", log=None):
        if profile not in PROFILES:
            raise ValueError(f"unknown profile {profile!r} (choose from {PROFILES})")
        self.profile = profile
        self.log = log
        self.wakeup: int | None = None          # hour we must be consulted at
        self.relay: dict[str, dict] = {}        # vehicle -> {"via", "return_at"}
        self.sent: dict[str, str] = {}          # incident -> vehicle we dispatched
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
        if action.kind == "dispatch":
            self.sent[action.target] = action.vehicle_id
        if action.kind in ("dispatch", "cancel") or action.tag == "reposition":
            self.relay.pop(action.vehicle_id, None)

    def on_rejected(self, action: Action) -> None:
        if action.tag == "relay":
            self.relay.pop(action.vehicle_id, None)

    def _recover(self, world: World) -> None:
        """Breakdown recovery: an incident we had covered lost its vehicle
        (force-cancelled when the vehicle went out of service). It is open
        again, so this round's planning will reassign it."""
        for inc_id, vid in list(self.sent.items()):
            inc = world.incidents.get(inc_id)
            if inc is None or inc["status"] != "open":
                del self.sent[inc_id]
                continue
            if inc["assigned_vehicle_id"] is None:
                v = world.vehicles.get(vid, {})
                why = v.get("oos_reason") or v.get("status", "?")
                if self.log:
                    self.log.decision(world.clock, "RECOVER", vid, inc_id,
                                      f"{vid} was force-cancelled ({why}); {inc_id} "
                                      f"({inc['people_at_risk']} people, deadline "
                                      f"H{inc['deadline']}) is unassigned again -> replanning now")
                del self.sent[inc_id]
        for vid in list(self.relay):
            if world.vehicles[vid]["status"] == "out_of_service":
                del self.relay[vid]

    # -- entry point ---------------------------------------------------------

    def decide(self, state: dict) -> list[Action]:
        world = World(state)
        self.wakeup = None
        if self.profile == "basic":
            return self._greedy_incidents(world, used=set(), care=None)
        self._recover(world)
        return self._decide_care(world)

    # -- profile: basic ------------------------------------------------------

    def _greedy_incidents(self, world: World, used: set[str],
                          care: tuple[CareNeed, str] | None) -> list[Action]:
        """Earliest deadline first; fastest feasible vehicle. When `care` is
        given as (need, care_vehicle), that vehicle is protected unless the
        incident saves more people than the care tick would cost."""
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
            extra = ""
            pick = min(options)
            if care is not None and pick[2] == care[1]:
                need, _ = care
                others = [o for o in options if o[2] != care[1]]
                if others:
                    pick = min(others)
                elif self._conflicts_with_care(world, pick, inc, need, used):
                    if inc["people_at_risk"] > need.value:
                        extra = (f"; overrides care visit for tick H{need.tick} "
                                 f"({inc['people_at_risk']} people > {need.value} residents)")
                    else:
                        self._note(("carewins", inc["id"]), world.clock, "SKIP",
                                   f"{inc['id']} ({inc['people_at_risk']} people) not taken: "
                                   f"only {pick[2]} can make it and it is needed for "
                                   f"care tick H{need.tick} ({need.value} residents)")
                        continue
            arrive, _, vid = pick
            used.add(vid)
            alts = ", ".join(f"{o[2]}@H{o[0]}" for o in sorted(options) if o[2] != vid) or "none"
            actions.append(Action(
                "dispatch", vid, inc["id"],
                f"EDF: {inc['people_at_risk']} people at {world.name(inc['location_id'])}, "
                f"arrive H{arrive} <= deadline H{inc['deadline']} "
                f"(slack {inc['deadline'] - arrive}h); alternatives: {alts}{extra}",
                tag="incident"))
        return actions

    def _conflicts_with_care(self, world: World, pick: tuple, inc: dict,
                             need: CareNeed, used: set[str]) -> bool:
        """Would sending `pick` to `inc` leave no vehicle able to cover the tick?"""
        arrive, _, vid = pick
        v = world.vehicles[vid]
        after = world.care_hours(inc["location_id"], v["speed"])
        if after is not None and arrive + after[0] <= need.tick:
            return False
        for other in world.vehicles.values():
            if other["id"] in used or other["id"] == vid or other["status"] == "out_of_service":
                continue
            if other["status"] == "available":
                t, loc = world.clock, other["location_id"]
            else:
                t, loc = other["mission"]["arrival"], other["mission"]["target"]
            ch = world.care_hours(loc, other["speed"])
            if ch is not None and t + ch[0] <= need.tick:
                return False
        return True

    # -- profile: care -------------------------------------------------------

    def _decide_care(self, world: World) -> list[Action]:
        actions: list[Action] = []
        used: set[str] = set()

        # Relay return leg: a vehicle that drove out from the home turns around.
        returning: list[int] = []
        for vid, r in list(self.relay.items()):
            v = world.vehicles[vid]
            if v["status"] == "available" and v["location_id"] == r["via"]:
                h = world.hours(r["via"], world.care_loc, v["speed"])
                del self.relay[vid]
                if h is None:
                    continue
                used.add(vid)
                returning.append(world.clock + h)
                actions.append(Action(
                    "move", vid, world.care_loc,
                    f"care relay return leg: back at the home H{world.clock + h} "
                    f"(target H{r['return_at']}) - a fresh arrival refreshes the visit window",
                    tag="care"))

        relay_visits = tuple(returning) + tuple(r["return_at"] for r in self.relay.values())
        need = world.care_need(extra_visits=relay_visits)
        care_vehicle = None
        if need is not None:
            care_vehicle = self._schedule_care(world, need, used, actions)
        protect = (need, care_vehicle) if care_vehicle else None
        actions += self._greedy_incidents(world, used, protect)
        return actions

    def _schedule_care(self, world: World, need: CareNeed, used: set[str],
                       actions: list[Action]) -> str | None:
        """Pick the care vehicle; send it when its latest departure hour comes."""
        cands = []
        for v in world.available():
            if v["id"] in used:
                continue
            ch = world.care_hours(v["location_id"], v["speed"])
            if ch is None:
                continue
            depart = need.tick - ch[0]
            if depart < world.clock:
                continue
            # least valuable vehicle for incidents: slowest, then shortest trip
            cands.append((v["speed"], ch[0], v["id"], depart, ch[1]))
        if not cands:
            if not self._care_reachable_later(world, need):
                self._note(("care-risk", need.tick), world.clock, "CARE-RISK",
                           f"care tick H{need.tick} at risk: no vehicle can reach "
                           f"{world.name(world.care_loc)} by then ({need.value} residents)")
            return None
        speed, hours, vid, depart, via = min(cands)
        if depart > world.clock:
            self.wakeup = depart
            return vid
        used.add(vid)
        actions.append(self._care_action(world, vid, need, hours, via))
        return None

    def _care_reachable_later(self, world: World, need: CareNeed) -> bool:
        for v in world.vehicles.values():
            if v["status"] != "busy":
                continue
            m = v["mission"]
            ch = world.care_hours(m["target"], v["speed"])
            if ch is not None and m["arrival"] + ch[0] <= need.tick:
                return True
        return False

    def _care_action(self, world: World, vid: str, need: CareNeed,
                     hours: int, via: str | None) -> Action:
        covers = f"covers tick H{need.tick} and the next window"
        if via is None:
            return Action("move", vid, world.care_loc,
                          f"care visit: latest departure for tick H{need.tick}; arrive "
                          f"exactly H{world.clock + hours} -> {covers} "
                          f"({need.value} residents at stake)", tag="care")
        self.relay[vid] = {"via": via, "return_at": world.clock + hours}
        return Action("move", vid, via,
                      f"care relay out-leg: {vid} is parked at the home and parking does "
                      f"not count; out to {world.name(via)} and back by H{world.clock + hours} "
                      f"-> {covers}", tag="relay")
