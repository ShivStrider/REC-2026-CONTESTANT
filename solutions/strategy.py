"""Dispatch strategy: turns one /state snapshot into a list of actions.

Three profiles, each building on the previous one, so their scores can be
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

  full   care, but assignments come from a small exhaustive search instead of
         greedy: every open incident + the next care visit are assigned to
         vehicle routes (including vehicles that will free up later, and
         two-stop chains) to maximise people saved. Adds the cancel rule
         (cancel a committed vehicle only if the whole plan saves strictly
         more people) and repositioning of idle vehicles toward where
         incidents have occurred so far.

The planner never sees the future: it only uses the snapshot it is given.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .roads import RoadMap

PROFILES = ("basic", "care", "full")

MAX_SEARCH_TASKS = 9         # tasks considered per search (earliest deadlines first)
SEARCH_NODE_BUDGET = 150_000  # safety cap on search size per decision round
REPOSITION_MIN_GAIN = 0.10   # move idle vehicle only if coverage cost drops >= 10%
                             # (moves are cheap: a repositioning trip can be cancelled
                             # at no people-cost when an incident needs the vehicle)
REPOSITION_MAX_HOURS = 10    # never send an idle vehicle on a longer repositioning trip
PRIOR_PEOPLE = 3.0           # every location starts as if 3 people had been at risk there
PRIOR_DEADLINES = (4, 6, 8, 10, 12, 16)  # assumed deadline lengths before any are seen

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


@dataclass(frozen=True)
class Task:
    id: str
    loc: str
    deadline: int
    value: int
    care: bool = False
    window_start: int = 0


@dataclass
class Resource:
    vid: str
    speed: float
    ready_t: int             # hour the vehicle is free to take a new task
    ready_loc: str           # where it is when free
    mode: str                # "idle" | "busy" | "cancelled"


@dataclass
class Leg:
    task: Task
    depart: int
    arrive: int
    via: str | None = None   # turnaround point for a care-home relay


@dataclass
class Plan:
    value: int = -1
    travel: int = 0
    routes: dict[str, list[Leg]] = field(default_factory=dict)


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
    def __init__(self, profile: str = "full", log=None):
        if profile not in PROFILES:
            raise ValueError(f"unknown profile {profile!r} (choose from {PROFILES})")
        self.profile = profile
        self.log = log
        self.wakeup: int | None = None          # hour we must be consulted at
        self.relay: dict[str, dict] = {}        # vehicle -> {"via", "return_at"}
        self.sent: dict[str, str] = {}          # incident -> vehicle we dispatched
        self.noted: set[tuple] = set()          # one-off notes already logged
        self._hold_deadline: dict[str, int] = {}  # vehicle -> care tick it must reach
        self._searched: set[str] = set()        # task ids the last search considered

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
        if self.profile == "care":
            return self._decide_care(world)
        return self._decide_full(world)

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

    # -- profile: full -------------------------------------------------------

    def _decide_full(self, world: World) -> list[Action]:
        base_res = self._resources(world)
        base_need = world.care_need()
        base_tasks = self._tasks(world, base_need)
        plan = self._search(world, base_res, base_tasks, base_need)

        # Cancel rule: try cancelling each committed vehicle (one at a time).
        # Accept only if the whole plan then saves strictly more people than
        # keeping the commitment. Sunk hours are already priced in, because
        # the cancelled vehicle restarts from its origin at the current hour.
        self._searched = {t.id for t in base_tasks}
        planned = {leg.task.id for legs in plan.routes.values() for leg in legs}
        left_out = [t for t in base_tasks if not t.care and t.id not in planned]
        cancel = None
        considered = []
        for r in base_res:
            if r.mode != "busy":
                continue
            v = world.vehicles[r.vid]
            m = v["mission"]
            keep_value = plan.value + self._commitment_value(world, v, base_need)
            res = [Resource(x.vid, x.speed, world.clock, m["origin"], "cancelled")
                   if x.vid == r.vid else x for x in base_res]
            need = world.care_need(exclude_vehicle=r.vid)
            tasks = self._tasks(world, need, freed=m.get("incident_id"))
            alt = self._search(world, res, tasks, need)
            considered.append((r.vid, m, alt.value, keep_value))
            if alt.value > keep_value and (cancel is None or alt.value - keep_value > cancel[2]):
                cancel = (r.vid, alt, alt.value - keep_value, keep_value, need, tasks)

        # Cancel consciously rejected: when an incident is going unserved, say
        # (once per mission) why no committed vehicle was pulled off its trip.
        if left_out and not cancel:
            for vid, m, alt_value, keep_value in considered:
                what = m["incident_id"] or f"move to {m['target']}"
                self._note(("keep", vid, m["depart"], m["target"]), world.clock, "KEEP",
                           f"considered cancelling {vid} ({what}) for "
                           f"{', '.join(t.id for t in left_out)}: replanned schedule would save "
                           f"{alt_value} vs {keep_value} if kept -> keep the commitment")

        actions: list[Action] = []
        need = base_need
        if cancel:
            vid, plan, gain, keep_value, need, tasks = cancel
            self._searched = {t.id for t in tasks}
            m = world.vehicles[vid]["mission"]
            what = m["incident_id"] or f"move to {m['target']}"
            actions.append(Action(
                "cancel", vid, None,
                f"cancel {what}: replanned schedule saves {plan.value} vs {keep_value} "
                f"if kept (+{gain}); {world.clock - m['depart']}h already driven are "
                f"lost, {vid} restarts from {world.name(m['origin'])}", tag="cancel"))

        actions += self._execute(world, plan, need, cancel[0] if cancel else None)
        return actions

    def _commitment_value(self, world: World, v: dict, need: CareNeed | None) -> int:
        """People an in-flight mission is still going to save."""
        m = v["mission"]
        value = 0
        if m["kind"] == "incident":
            inc = world.incidents[m["incident_id"]]
            if m["arrival"] <= inc["deadline"]:
                value += inc["people_at_risk"]
        if m["target"] == world.care_loc:
            excl = world.care_need(exclude_vehicle=v["id"])
            if excl and excl.window_start <= m["arrival"] <= excl.tick and (
                    need is None or need.tick != excl.tick):
                value += excl.value
        return value

    def _resources(self, world: World) -> list[Resource]:
        out = []
        for v in world.vehicles.values():
            if v["status"] == "available":
                out.append(Resource(v["id"], v["speed"], world.clock, v["location_id"], "idle"))
            elif v["status"] == "busy" and v["mission"]:
                m = v["mission"]
                out.append(Resource(v["id"], v["speed"], max(m["arrival"], world.clock),
                                    m["target"], "busy"))
        return out

    def _tasks(self, world: World, need: CareNeed | None,
               freed: str | None = None) -> list[Task]:
        tasks = [Task(i["id"], i["location_id"], i["deadline"], i["people_at_risk"])
                 for i in world.open_unassigned()]
        if freed and freed not in {t.id for t in tasks}:
            i = world.incidents[freed]
            if i["status"] == "open":
                tasks.append(Task(i["id"], i["location_id"], i["deadline"], i["people_at_risk"]))
        tasks.sort(key=lambda t: (t.deadline, -t.value))
        # the care visit is always considered; incidents fill the remaining slots
        tasks = tasks[:MAX_SEARCH_TASKS - (need is not None)]
        if need is not None:
            tasks.append(Task("CARE", world.care_loc, need.tick, need.value,
                              care=True, window_start=need.window_start))
            tasks.sort(key=lambda t: (t.deadline, -t.value))
        return tasks

    def _leg(self, world: World, r: Resource, t: int, loc: str, task: Task) -> Leg | None:
        if task.care:
            ch = world.care_hours(loc, r.speed)
            if ch is None or t + ch[0] > task.deadline:
                return None
            # leave as late as possible so we arrive exactly at the tick hour
            return Leg(task, task.deadline - ch[0], task.deadline, ch[1])
        h = world.hours(loc, task.loc, r.speed)
        if h is None or t + h > task.deadline:
            return None
        return Leg(task, t, t + h)

    def _search(self, world: World, resources: list[Resource],
                tasks: list[Task], need: CareNeed | None) -> Plan:
        """Exhaustive branch-and-bound over 'which vehicle takes which task,
        in what order'. Score: people saved, then least total driving."""
        best = Plan()
        cur = {r.vid: (r.ready_t, r.ready_loc) for r in resources}
        routes: dict[str, list[Leg]] = {r.vid: [] for r in resources}
        n = len(tasks)
        # suffix[i] = most people tasks i.. could still add (the bound used to prune).
        # An incident at the care home can also earn the care bonus, so count it.
        care_bonus = need.value if need else 0
        suffix = [0] * (n + 1)
        for i in range(n - 1, -1, -1):
            at_home = not tasks[i].care and tasks[i].loc == world.care_loc
            suffix[i] = suffix[i + 1] + tasks[i].value + (care_bonus if at_home else 0)
        nodes = 0

        def dfs(i: int, value: int, travel: int, care_done: bool) -> None:
            nonlocal best, nodes
            nodes += 1
            if value + suffix[i] < best.value or nodes > SEARCH_NODE_BUDGET:
                return
            if i == n:
                if value > best.value or (value == best.value and travel < best.travel):
                    best = Plan(value, travel, {k: list(v) for k, v in routes.items()})
                return
            task = tasks[i]
            if task.care and care_done:
                dfs(i + 1, value, travel, care_done)
                return
            options = []
            for r in resources:
                t, loc = cur[r.vid]
                leg = self._leg(world, r, t, loc, task)
                if leg is not None:
                    options.append((leg.arrive - t, r, leg))
            options.sort(key=lambda o: (o[0], o[1].speed))
            for hours, r, leg in options:
                gain = task.value
                now_care = care_done or task.care
                if (not task.care and need and not care_done and task.loc == world.care_loc
                        and need.window_start <= leg.arrive <= need.tick):
                    gain += need.value     # an incident at the home also counts as a visit
                    now_care = True
                saved = cur[r.vid]
                cur[r.vid] = (leg.arrive, task.loc)
                routes[r.vid].append(leg)
                dfs(i + 1, value + gain, travel + hours, now_care)
                routes[r.vid].pop()
                cur[r.vid] = saved
            dfs(i + 1, value, travel, care_done)

        dfs(0, 0, 0, False)
        return best

    def _execute(self, world: World, plan: Plan, need: CareNeed | None,
                 cancelled: str | None) -> list[Action]:
        self._hold_deadline = {}
        actions: list[Action] = []
        planned: set[str] = set()
        holding: set[str] = set()
        anchors: list[tuple[str, float]] = []   # (location, speed) of committed vehicles

        for vid, legs in plan.routes.items():
            for leg in legs:
                planned.add(leg.task.id)
            v = world.vehicles[vid]
            free_now = v["status"] == "available" or vid == cancelled
            if not legs:
                if not free_now:
                    anchors.append((v["mission"]["target"], v["speed"]))
                continue
            anchors.append((legs[-1].task.loc, v["speed"]))
            if not free_now:
                continue
            leg = legs[0]
            chain = "".join(f"; then {l.task.id} (arrive H{l.arrive})" for l in legs[1:])
            if leg.task.care:
                if leg.depart > world.clock:
                    holding.add(vid)
                    self._hold_deadline[vid] = leg.arrive
                    self.wakeup = leg.depart if self.wakeup is None else min(self.wakeup, leg.depart)
                    self._note(("hold", vid, need.tick), world.clock, "HOLD",
                               f"{vid} reserved for care tick H{need.tick}: departs "
                               f"H{leg.depart} to arrive exactly at the tick{chain}")
                    continue
                act = self._care_action(world, vid, need, leg.arrive - world.clock, leg.via)
                act.reason += chain
                actions.append(act)
                continue
            inc = world.incidents[leg.task.id]
            alts = []
            for other in plan.routes:
                if other == vid:
                    continue
                ov = world.vehicles[other]
                ot = world.clock if ov["status"] == "available" else ov["mission"]["arrival"]
                ol = ov["location_id"] if ov["status"] == "available" else ov["mission"]["target"]
                h = world.hours(ol, inc["location_id"], ov["speed"])
                if h is not None and ot + h <= inc["deadline"]:
                    alts.append(f"{other}@H{ot + h}")
            actions.append(Action(
                "dispatch", vid, inc["id"],
                f"plan: {inc['people_at_risk']} people at {world.name(inc['location_id'])}, "
                f"arrive H{leg.arrive} <= deadline H{inc['deadline']} "
                f"(slack {inc['deadline'] - leg.arrive}h); "
                f"other vehicles in time: {', '.join(alts) or 'none'}{chain}; "
                f"round plan saves {plan.value}", tag="incident"))

        self._explain_unserved(world, plan, planned, need)
        actions += self._reposition(world, plan, holding, anchors, cancelled)
        return actions

    def _explain_unserved(self, world: World, plan: Plan, planned: set[str],
                          need: CareNeed | None) -> None:
        for inc in world.open_unassigned():
            if inc["id"] in planned:
                continue
            reachable = False
            for r in self._resources(world):
                h = world.hours(r.ready_loc, inc["location_id"], r.speed)
                if h is not None and r.ready_t + h <= inc["deadline"]:
                    reachable = True
            if inc["id"] not in self._searched and reachable:
                self._note(("deferred", inc["id"]), world.clock, "DEFERRED",
                           f"{inc['id']} ({inc['people_at_risk']} people, deadline H{inc['deadline']}) "
                           f"not planned this round: only the {MAX_SEARCH_TASKS} earliest-deadline "
                           f"tasks are searched; it is reconsidered next round")
            elif not reachable:
                self._note(("unreachable", inc["id"]), world.clock, "UNREACHABLE",
                           f"{inc['id']} ({inc['people_at_risk']} people, deadline "
                           f"H{inc['deadline']}) at {world.name(inc['location_id'])}: no vehicle "
                           f"can arrive in time - not dispatching (a late vehicle saves nobody)")
            else:
                self._note(("traded", inc["id"]), world.clock, "TRADE-OFF",
                           f"{inc['id']} ({inc['people_at_risk']} people) left unserved: the "
                           f"vehicles that could reach it save more elsewhere "
                           f"(round plan saves {plan.value})")
        if need and "CARE" not in planned and not any(
                l.task.loc == world.care_loc and need.window_start <= l.arrive <= need.tick
                for legs in plan.routes.values() for l in legs):
            self._note(("care-skip", need.tick), world.clock, "CARE-RISK",
                       f"care tick H{need.tick} ({need.value} residents) not covered: "
                       f"no vehicle can reach the home in time without abandoning "
                       f"incidents that save more people")

    def _reposition(self, world: World, plan: Plan, holding: set[str],
                    anchors: list[tuple[str, float]], cancelled: str | None) -> list[Action]:
        """Park idle vehicles where they minimise *expected people missed* if
        the next incidents look like the past ones: each location is weighted
        by people seen there, and a response of h hours fails with the share
        of observed deadlines shorter than h. Learned only from incidents
        already seen. A vehicle reserved for a later care visit may move too,
        as long as it can still reach the home by its tick."""
        idle = [world.vehicles[vid] for vid, legs in plan.routes.items()
                if (not legs or vid in holding)
                and (world.vehicles[vid]["status"] == "available" or vid == cancelled)]
        if not idle:
            return []
        weight = {loc: PRIOR_PEOPLE for loc in world.map.location_ids}
        offsets = list(PRIOR_DEADLINES)
        for inc in world.incidents.values():
            weight[inc["location_id"]] = weight.get(inc["location_id"], 0) + inc["people_at_risk"]
            offsets.append(inc["deadline"] - inc["created_at"])
        offsets.sort()

        def miss_share(h: int) -> float:
            return sum(1 for o in offsets if o < h) / len(offsets)

        def cost(fleet: list[tuple[str, float]]) -> float:
            total = 0.0
            for loc, w in weight.items():
                hs = [world.hours(at, loc, speed) for at, speed in fleet]
                hs = [h for h in hs if h is not None]
                if not hs:
                    total += w
                    continue
                best = min(hs)
                total += w * (miss_share(best) + 0.001 * best)   # tiny tie-break on hours
            return total

        actions = []
        idle.sort(key=lambda v: (v["id"] in holding, -v["speed"]))
        placed = list(anchors)
        for i, v in enumerate(idle):
            here = v["location_id"] if v["status"] == "available" else v["mission"]["origin"]
            rest = [(x["location_id"], x["speed"]) for x in idle[i + 1:]]
            stay = cost(placed + [(here, v["speed"])] + rest)
            best_loc, best_cost = here, stay
            for loc in world.map.location_ids:
                if loc == here:
                    continue
                h = world.hours(here, loc, v["speed"])
                if h is None or h > REPOSITION_MAX_HOURS:
                    continue
                if v["id"] in holding:
                    back = world.care_hours(loc, v["speed"])
                    if back is None or world.clock + h + back[0] > self._hold_deadline[v["id"]]:
                        continue
                c = cost(placed + [(loc, v["speed"])] + rest)
                if c < best_cost:
                    best_loc, best_cost = loc, c
            gain = stay - best_cost
            if best_loc != here and gain >= REPOSITION_MIN_GAIN * stay:
                h = world.hours(here, best_loc, v["speed"])
                duty = (f"; still reaches the care home by H{self._hold_deadline[v['id']]}"
                        if v["id"] in holding else "")
                actions.append(Action(
                    "move", v["id"], best_loc,
                    f"reposition {v['id']} to {world.name(best_loc)} (arrive H{world.clock + h}): "
                    f"expected people missed if incidents follow the pattern so far "
                    f"{stay:.1f} -> {best_cost:.1f}{duty}", tag="reposition"))
                placed.append((best_loc, v["speed"]))
            else:
                placed.append((here, v["speed"]))
        return actions
