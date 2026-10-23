"""Core simulation engine for REC 2026 — Emergency Response: 90 Days.

One Simulation instance = one run of one scenario. Pure standard library.

Core rules (see docs/TEAM_GUIDE.md):
- Integer-hour clock; a dispatch's arrival time is fixed when dispatched.
- Arrive at an incident at or before its deadline -> everyone is saved.
  Arrive after (or never) -> everyone is lost when the deadline passes;
  the vehicle is still occupied until it arrives.
- A vehicle may respond to at most one open incident at a time.
- The care home (chronic) loses `loss_per_tick` residents every
  `interval_hours` unless a vehicle arrived at it during the preceding
  interval. Any mission arrival (response or reposition) counts as a visit.
- Road status affects travel for NEW dispatches only; en-route arrivals
  are not recalculated. Cancel returns the vehicle to its dispatch origin.
"""
from __future__ import annotations

import copy
import math

from . import travel

VALID_EVENT_TYPES = {"incident", "road_status", "vehicle_status"}
VALID_ROAD_STATUS = {"open", "restricted", "closed"}
VALID_VEHICLE_STATUS = {"available", "out_of_service"}


class SimulationError(Exception):
    """Invalid scenario data or invalid action."""


# --------------------------------------------------------------------------
# scenario validation
# --------------------------------------------------------------------------

def validate_scenario(s: dict) -> None:
    def fail(msg: str) -> None:
        raise SimulationError(f"invalid scenario: {msg}")

    if not isinstance(s, dict):
        fail("not an object")
    for key in ("horizon_hours", "locations", "roads", "vehicles"):
        if key not in s:
            fail(f"missing '{key}'")
    horizon = s["horizon_hours"]
    if not isinstance(horizon, int) or horizon < 1:
        fail("horizon_hours must be a positive integer")

    def unique(items: list, label: str) -> dict:
        out = {}
        for it in items:
            if "id" not in it:
                fail(f"{label} entry missing id")
            if it["id"] in out:
                fail(f"duplicate {label} id '{it['id']}'")
            out[it["id"]] = it
        return out

    locs = unique(s["locations"], "location")
    roads = unique(s["roads"], "road")
    vehs = unique(s["vehicles"], "vehicle")

    for r in roads.values():
        for k in ("from_location_id", "to_location_id", "hours", "status"):
            if k not in r:
                fail(f"road {r['id']} missing '{k}'")
        if r["from_location_id"] not in locs or r["to_location_id"] not in locs:
            fail(f"road {r['id']} references unknown location")
        if not isinstance(r["hours"], (int, float)) or r["hours"] <= 0:
            fail(f"road {r['id']} hours must be > 0")
        if r["status"] not in VALID_ROAD_STATUS:
            fail(f"road {r['id']} bad status '{r['status']}'")

    for v in vehs.values():
        if "speed" not in v or not isinstance(v["speed"], (int, float)) or v["speed"] <= 0:
            fail(f"vehicle {v['id']} speed must be > 0")
        if v.get("location_id") not in locs:
            fail(f"vehicle {v['id']} references unknown location")
        if v.get("status", "available") not in VALID_VEHICLE_STATUS:
            fail(f"vehicle {v['id']} bad status")

    seen_incident_ids: set[str] = set()
    for i, e in enumerate(s.get("events", [])):
        where = f"events[{i}]"
        if "time" not in e or "type" not in e or "data" not in e:
            fail(f"{where} needs time, type, data")
        if not isinstance(e["time"], int) or not 0 <= e["time"] <= horizon:
            fail(f"{where} time out of range")
        if e["type"] not in VALID_EVENT_TYPES:
            fail(f"{where} unknown type '{e['type']}'")
        d = e["data"]
        if e["type"] == "incident":
            for k in ("id", "title", "location_id", "people_at_risk", "deadline_offset_hours"):
                if k not in d:
                    fail(f"{where} incident missing '{k}'")
            if d["id"] in seen_incident_ids:
                fail(f"{where} duplicate incident id '{d['id']}'")
            seen_incident_ids.add(d["id"])
            if d["location_id"] not in locs:
                fail(f"{where} incident references unknown location")
            if not isinstance(d["people_at_risk"], int) or d["people_at_risk"] < 1:
                fail(f"{where} people_at_risk must be >= 1")
            off = d["deadline_offset_hours"]
            if not isinstance(off, int) or off < 1:
                fail(f"{where} deadline_offset_hours must be >= 1")
            if e["time"] + off > horizon:
                fail(f"{where} incident deadline falls after horizon")
        elif e["type"] == "road_status":
            if d.get("road_id") not in roads:
                fail(f"{where} unknown road")
            if d.get("status") not in VALID_ROAD_STATUS:
                fail(f"{where} bad road status")
        elif e["type"] == "vehicle_status":
            if d.get("vehicle_id") not in vehs:
                fail(f"{where} unknown vehicle")
            if d.get("status") not in VALID_VEHICLE_STATUS:
                fail(f"{where} bad vehicle status")

    c = s.get("chronic")
    if c is not None:
        for k in ("location_id", "interval_hours", "loss_per_tick", "initial_people"):
            if k not in c:
                fail(f"chronic missing '{k}'")
        if c["location_id"] not in locs:
            fail("chronic references unknown location")
        if c["interval_hours"] < 1 or c["loss_per_tick"] < 1 or c["initial_people"] < 1:
            fail("chronic numbers must be >= 1")


# --------------------------------------------------------------------------
# simulation
# --------------------------------------------------------------------------

class Simulation:
    def __init__(self, scenario: dict):
        validate_scenario(scenario)
        self.scenario = scenario
        self.scenario_id = scenario.get("scenario_id", "")
        self.title = scenario.get("title", "")
        self.narrative = scenario.get("narrative", "")
        self.horizon = scenario["horizon_hours"]
        self.clock = 0
        self.locations = {l["id"]: dict(l) for l in scenario["locations"]}
        self.roads = {r["id"]: dict(r) for r in scenario["roads"]}
        self.vehicles: dict[str, dict] = {}
        for v in scenario["vehicles"]:
            vv = dict(v)
            vv.setdefault("status", "available")
            vv["mission"] = None
            self.vehicles[vv["id"]] = vv
        self.incidents: dict[str, dict] = {}
        self._events = sorted(
            ({"time": e["time"], "type": e["type"], "data": dict(e["data"])}
             for e in scenario.get("events", [])),
            key=lambda e: e["time"],
        )
        self._event_idx = 0
        c = scenario.get("chronic")
        self.chronic: dict | None = None
        if c is not None:
            self.chronic = {
                **c,
                "last_visit": -1,
                "people_remaining": c["initial_people"],
                "next_tick": c["interval_hours"],
            }
        self.log: list[dict] = []
        self.people_saved = 0
        self.people_lost_incidents = 0
        self.people_lost_chronic = 0
        self._run_until(0)

    # -- helpers ------------------------------------------------------------

    @property
    def day(self) -> int:
        return self.clock // 24 + 1

    def _log(self, kind: str, text: str) -> None:
        self.log.append({"hour": self.clock, "day": self.day, "kind": kind, "text": text})

    def _vehicle(self, vehicle_id: str) -> dict:
        v = self.vehicles.get(vehicle_id)
        if v is None:
            raise SimulationError(f"unknown vehicle '{vehicle_id}'")
        return v

    def _incident(self, incident_id: str) -> dict:
        inc = self.incidents.get(incident_id)
        if inc is None:
            raise SimulationError(f"unknown incident '{incident_id}'")
        return inc

    def _next_pending(self) -> int | None:
        """Smallest future time at which something happens, else None."""
        times: list[int] = []
        if self._event_idx < len(self._events):
            times.append(self._events[self._event_idx]["time"])
        for v in self.vehicles.values():
            if v["mission"] is not None:
                times.append(v["mission"]["arrival"])
        for inc in self.incidents.values():
            if inc["status"] == "open":
                times.append(inc["deadline"])
        if self.chronic is not None and self.chronic["people_remaining"] > 0:
            if self.chronic["next_tick"] <= self.horizon:
                times.append(self.chronic["next_tick"])
        future = [t for t in times if t >= self.clock]
        return min(future) if future else None

    # -- event processing ---------------------------------------------------

    def _run_until(self, target: int) -> None:
        if target < self.clock:
            raise SimulationError(f"cannot move clock backwards ({self.clock} -> {target})")
        while True:
            nxt = self._next_pending()
            if nxt is None or nxt > target:
                break
            self.clock = nxt
            self._process_arrivals()
            self._process_deadlines()
            self._process_chronic()
            self._process_events()
        self.clock = target

    def _process_arrivals(self) -> None:
        for v in list(self.vehicles.values()):
            m = v["mission"]
            if m is None or m["arrival"] != self.clock:
                continue
            v["mission"] = None
            v["status"] = "available"
            v["location_id"] = m["target"]
            if m["kind"] == "incident":
                inc = self.incidents[m["incident_id"]]
                if inc["status"] == "open":
                    inc["status"] = "resolved"
                    inc["resolved_at"] = self.clock
                    self.people_saved += inc["people_at_risk"]
                    self._log("incident_resolved",
                              f"{v['id']} arrived at {inc['id']} — "
                              f"{inc['people_at_risk']} people saved")
                else:
                    self._log("late_arrival",
                              f"{v['id']} arrived at {inc['id']} after its outcome — wasted trip")
            else:
                self._log("arrived", f"{v['id']} arrived at {m['target']}")
            if self.chronic is not None and v["location_id"] == self.chronic["location_id"]:
                self.chronic["last_visit"] = self.clock

    def _process_deadlines(self) -> None:
        for inc in self.incidents.values():
            if inc["status"] == "open" and inc["deadline"] == self.clock:
                inc["status"] = "missed"
                self.people_lost_incidents += inc["people_at_risk"]
                self._log("incident_missed",
                          f"DEADLINE {inc['id']} — {inc['people_at_risk']} people lost")

    def _process_chronic(self) -> None:
        c = self.chronic
        if c is None or c["people_remaining"] <= 0 or c["next_tick"] != self.clock:
            return
        interval = c["interval_hours"]
        if c["last_visit"] < self.clock - interval:
            lost = min(c["loss_per_tick"], c["people_remaining"])
            c["people_remaining"] -= lost
            self.people_lost_chronic += lost
            self._log("chronic_loss",
                      f"{c.get('name', 'care home')} — {lost} resident(s) lost "
                      f"(no vehicle visit in {interval}h; {c['people_remaining']} remaining)")
        else:
            self._log("chronic_ok",
                      f"{c.get('name', 'care home')} — visit within window, no losses")
        while c["next_tick"] <= self.clock:
            c["next_tick"] += interval

    def _process_events(self) -> None:
        while self._event_idx < len(self._events) and self._events[self._event_idx]["time"] == self.clock:
            e = self._events[self._event_idx]
            self._event_idx += 1
            d = e["data"]
            if e["type"] == "incident":
                inc = {
                    "id": d["id"],
                    "title": d["title"],
                    "description": d.get("description", ""),
                    "location_id": d["location_id"],
                    "people_at_risk": d["people_at_risk"],
                    "deadline": e["time"] + d["deadline_offset_hours"],
                    "status": "open",
                    "assigned_vehicle_id": None,
                    "created_at": e["time"],
                    "resolved_at": None,
                }
                self.incidents[inc["id"]] = inc
                self._log("incident_open",
                          f"NEW INCIDENT {inc['id']}: {inc['title']} — "
                          f"{inc['people_at_risk']} people at {inc['location_id']}, "
                          f"deadline hour {inc['deadline']} (day {inc['deadline'] // 24 + 1})")
            elif e["type"] == "road_status":
                r = self.roads[d["road_id"]]
                r["status"] = d["status"]
                r["reason"] = d.get("reason", "")
                self._log("road_status",
                          f"ROAD {r['id']} {r['name']} is now {d['status'].upper()}"
                          + (f" — {d['reason']}" if d.get("reason") else ""))
            elif e["type"] == "vehicle_status":
                v = self.vehicles[d["vehicle_id"]]
                if d["status"] == "out_of_service":
                    if v["mission"] is not None:
                        self._force_cancel(v)
                    v["status"] = "out_of_service"
                    v["oos_reason"] = d.get("reason", "")
                    self._log("vehicle_status",
                              f"{v['id']} OUT OF SERVICE"
                              + (f" — {d['reason']}" if d.get("reason") else "")
                              + " (mission cancelled)")
                else:
                    if v["status"] == "out_of_service":
                        v["status"] = "available"
                        v.pop("oos_reason", None)
                        self._log("vehicle_status", f"{v['id']} back in service")
                    else:
                        self._log("vehicle_status",
                                  f"{v['id']} availability event ignored (status: {v['status']})")

    def _force_cancel(self, v: dict) -> None:
        m = v["mission"]
        if m["kind"] == "incident":
            inc = self.incidents[m["incident_id"]]
            if inc.get("assigned_vehicle_id") == v["id"]:
                inc["assigned_vehicle_id"] = None
        v["location_id"] = m["origin"]
        v["mission"] = None

    # -- actions ------------------------------------------------------------

    def dispatch(self, vehicle_id: str, incident_id: str) -> dict:
        v = self._vehicle(vehicle_id)
        if v["status"] != "available":
            raise SimulationError(
                f"vehicle {vehicle_id} is not available (status: {v['status']})")
        inc = self._incident(incident_id)
        if inc["status"] != "open":
            raise SimulationError(f"incident {incident_id} is {inc['status']}, not open")
        if inc["assigned_vehicle_id"] is not None:
            raise SimulationError(
                f"incident {incident_id} already has {inc['assigned_vehicle_id']} responding")
        hours = travel.shortest_hours(
            self.roads, v["location_id"], inc["location_id"], v["speed"])
        if hours is None:
            raise SimulationError(
                f"no route from {v['location_id']} to {inc['location_id']} (roads closed)")
        arrival = self.clock + math.ceil(hours)
        v["status"] = "busy"
        v["mission"] = {
            "kind": "incident",
            "incident_id": inc["id"],
            "origin": v["location_id"],
            "target": inc["location_id"],
            "depart": self.clock,
            "arrival": arrival,
        }
        inc["assigned_vehicle_id"] = v["id"]
        self._log("dispatch",
                  f"{v['id']} dispatched to {inc['id']} — "
                  f"{arrival - self.clock}h travel, arrives hour {arrival}")
        if arrival == self.clock:
            self._process_arrivals()
        result = {
            "ok": True,
            "vehicle_id": vehicle_id,
            "incident_id": incident_id,
            "arrival_hour": arrival,
            "deadline_hour": inc["deadline"],
            "arrives_before_deadline": arrival <= inc["deadline"],
        }
        if arrival > inc["deadline"]:
            result["warning"] = (
                f"arrives hour {arrival}, AFTER deadline hour {inc['deadline']} — "
                "nobody will be saved and the vehicle is committed until arrival")
        return result

    def move(self, vehicle_id: str, location_id: str) -> dict:
        v = self._vehicle(vehicle_id)
        if v["status"] != "available":
            raise SimulationError(
                f"vehicle {vehicle_id} is not available (status: {v['status']})")
        if location_id not in self.locations:
            raise SimulationError(f"unknown location '{location_id}'")
        if location_id == v["location_id"]:
            raise SimulationError(
                f"vehicle {vehicle_id} is already at {location_id} — "
                "a real trip out and back is required to refresh a visit")
        hours = travel.shortest_hours(
            self.roads, v["location_id"], location_id, v["speed"])
        if hours is None:
            raise SimulationError(
                f"no route from {v['location_id']} to {location_id} (roads closed)")
        arrival = self.clock + math.ceil(hours)
        v["status"] = "busy"
        v["mission"] = {
            "kind": "move",
            "incident_id": None,
            "origin": v["location_id"],
            "target": location_id,
            "depart": self.clock,
            "arrival": arrival,
        }
        self._log("move",
                  f"{v['id']} repositioning to {location_id} — "
                  f"{arrival - self.clock}h travel, arrives hour {arrival}")
        if arrival == self.clock:
            self._process_arrivals()
        return {
            "ok": True,
            "vehicle_id": vehicle_id,
            "location_id": location_id,
            "arrival_hour": arrival,
        }

    def cancel(self, vehicle_id: str) -> dict:
        v = self._vehicle(vehicle_id)
        if v["status"] != "busy" or v["mission"] is None:
            raise SimulationError(f"vehicle {vehicle_id} has no active mission to cancel")
        m = v["mission"]
        wasted = self.clock - m["depart"]
        if m["kind"] == "incident":
            inc = self.incidents[m["incident_id"]]
            if inc.get("assigned_vehicle_id") == v["id"]:
                inc["assigned_vehicle_id"] = None
            target_desc = m["incident_id"]
        else:
            target_desc = m["target"]
        v["location_id"] = m["origin"]
        v["status"] = "available"
        v["mission"] = None
        self._log("cancel",
                  f"{v['id']} mission to {target_desc} cancelled — "
                  f"{wasted}h wasted, back at {m['origin']}")
        return {"ok": True, "vehicle_id": vehicle_id, "hours_wasted": wasted}

    def advance(self, to_hour: int | None = None) -> dict:
        if to_hour is None:
            nxt = self._next_pending()
            target = nxt if nxt is not None else self.horizon
        else:
            if not isinstance(to_hour, int):
                raise SimulationError("to_hour must be an integer")
            if to_hour < self.clock:
                raise SimulationError(
                    f"to_hour {to_hour} is in the past (clock is {self.clock})")
            if to_hour > self.horizon:
                raise SimulationError(
                    f"to_hour {to_hour} is beyond horizon {self.horizon}")
            target = to_hour
        before = len(self.log)
        self._run_until(target)
        return {
            "ok": True,
            "clock": self.clock,
            "day": self.day,
            "processed": self.log[before:],
        }

    # -- views --------------------------------------------------------------

    def state(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "title": self.title,
            "narrative": self.narrative,
            "clock": self.clock,
            "day": self.day,
            "horizon_hours": self.horizon,
            "locations": list(self.locations.values()),
            "roads": list(self.roads.values()),
            "vehicles": list(self.vehicles.values()),
            "incidents": list(self.incidents.values()),
            "chronic": self.chronic,
            "score": {
                "people_saved": self.people_saved,
                "people_lost_incidents": self.people_lost_incidents,
                "people_lost_chronic": self.people_lost_chronic,
            },
            "log": self.log,
        }

    def report(self) -> dict:
        details = sorted(self.incidents.values(), key=lambda i: i["deadline"])
        chronic_remaining = self.chronic["people_remaining"] if self.chronic else 0
        chronic_total = self.chronic["initial_people"] if self.chronic else 0
        return {
            "scenario_id": self.scenario_id,
            "title": self.title,
            "clock": self.clock,
            "day": self.day,
            "horizon_hours": self.horizon,
            "complete": self.clock >= self.horizon,
            "people_saved": self.people_saved,
            "people_lost_incidents": self.people_lost_incidents,
            "people_lost_chronic": self.people_lost_chronic,
            "people_lost_total": self.people_lost_incidents + self.people_lost_chronic,
            "chronic_remaining": chronic_remaining,
            "people_at_risk_total": (sum(i["people_at_risk"]
                                         for i in details) + chronic_total),
            "people_saved_total": self.people_saved + chronic_remaining,
            "incidents_total": len(self.incidents),
            "incidents_resolved": sum(1 for i in details if i["status"] == "resolved"),
            "incidents_missed": sum(1 for i in details if i["status"] == "missed"),
            "incidents_open": sum(1 for i in details if i["status"] == "open"),
            "incidents": details,
            "chronic": self.chronic,
        }

    # -- planning support (rebuild a private simulation from a public state) --

    def clone(self) -> "Simulation":
        return copy.deepcopy(self)

    @classmethod
    def from_public_state(cls, state: dict) -> "Simulation":
        """Rebuild a planning copy from GET /state. Future events are unknown
        (teams never see them), so the event queue starts empty."""
        obj = cls.__new__(cls)
        obj.scenario = {}
        obj.scenario_id = state.get("scenario_id", "")
        obj.title = state.get("title", "")
        obj.narrative = state.get("narrative", "")
        obj.horizon = state["horizon_hours"]
        obj.clock = state["clock"]
        obj.locations = {l["id"]: dict(l) for l in state["locations"]}
        obj.roads = {r["id"]: dict(r) for r in state["roads"]}
        obj.vehicles = {v["id"]: copy.deepcopy(v) for v in state["vehicles"]}
        obj.incidents = {i["id"]: copy.deepcopy(i) for i in state["incidents"]}
        obj._events = []
        obj._event_idx = 0
        obj.chronic = copy.deepcopy(state.get("chronic"))
        obj.log = []
        score = state.get("score", {})
        obj.people_saved = score.get("people_saved", 0)
        obj.people_lost_incidents = score.get("people_lost_incidents", 0)
        obj.people_lost_chronic = score.get("people_lost_chronic", 0)
        return obj
