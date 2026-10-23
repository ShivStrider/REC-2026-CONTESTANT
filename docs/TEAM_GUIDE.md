# Team Guide — Emergency Response: 90 Days

Everything you need to write a dispatch planner against the REC 2026
simulation engine. One document, one API, works identically for the 7-day
training scenario and the 90-day judging scenario.

- Python 3.10+ standard library only — no pip installs.
- The engine is a local HTTP server. Your planner is any program (any
  language) that speaks JSON over HTTP.
- **The engine owns the clock.** You never see the future: incidents appear
  in `/state` only when they open, and road/vehicle disruptions show up as
  changes to the roads and vehicles you already know.

---

## 1. Starting the engine

```bash
python3 -m engine.server --scenario 7day --port 8600   # training
python3 -m engine.server --scenario 90day --port 8600  # judging (identical API)
```

| Endpoint | Method | Body | Returns |
|---|---|---|---|
| `/health` | GET | — | `{ok, clock, scenario}` |
| `/state` | GET | — | `{ok, state}` — full world, no future events |
| `/report` | GET | — | `{ok, report}` — scoring summary (works mid-run) |
| `/dispatch` | POST | `{vehicle_id, incident_id}` | dispatch result |
| `/move` | POST | `{vehicle_id, location_id}` | move result |
| `/cancel` | POST | `{vehicle_id}` | cancel result |
| `/advance` | POST | `{}` or `{to_hour}` | processed log slice |
| `/reset` | POST | `{scenario}` | restart from hour 0 |

Errors are HTTP 400 with `{"ok": false, "error": "..."}`. A rejected action
never changes the world — fix the request and try the next decision round.

An example stdlib client is shipped as `solutions/client.py`; a full planner
loop using it is `solutions/good_care.py`.

---

## 2. The decision loop

```
while clock < horizon:
    state = GET /state
    actions = decide(state)          # your code
    for a in actions: POST /dispatch | /move | /cancel
    POST /advance                    # engine processes time until the
                                     # next arrival, deadline, chronic tick
                                     # or scripted event — then returns
```

`/advance` with an empty body jumps to the next pending moment. Pass
`{"to_hour": 123}` to jump to a specific hour (never backwards, never past
the horizon). Every action you issue is applied **before** the clock moves,
so all actions in one round share the same `clock`.

---

## 3. The state object

`GET /state` returns `{ "ok": true, "state": { ... } }`. Every field:

```
state
├── scenario_id          "7day" | "90day"
├── title, narrative     flavour text
├── clock                current hour (int, starts at 0)
├── day                  clock // 24 + 1
├── horizon_hours        end of run (168 or 2160)
├── locations[]          see 3.1
├── roads[]              see 3.2
├── vehicles[]           see 3.3
├── incidents[]          every incident opened so far, see 3.4
├── chronic              the care home, or null — see 3.5
├── score                running totals — see 3.6
└── log[]                every engine log line so far — see 3.7
```

### 3.1 Location

```
{"id": "LOC-1", "name": "Community Hall", "kind": "town"}
```

`kind` is informational (`town`, `care`, `residential`, `depot`, `rural`,
`dock`). Exactly one location carries the chronic care home.

### 3.2 Road

```
{"id": "RD-1", "name": "Main Street", "from_location_id": "LOC-1",
 "to_location_id": "LOC-2", "hours": 4, "status": "open",
 "reason": "..."}          # reason appears only after a status change
```

Roads are **undirected**; travel time is the same both ways.

| `status` | Effect on new dispatches |
|---|---|
| `open` | `hours` as written |
| `restricted` | `hours × 1.5` |
| `closed` | impassable — dispatch/move raises an error |

Status changes affect **new dispatches only**; vehicles already en route keep
their original arrival hour.

### 3.3 Vehicle

```
{"id": "VEH-02", "name": "Ambulance", "speed": 1.25,
 "location_id": "LOC-1", "status": "available",
 "mission": null,
 "oos_reason": "..."}      # present only while out_of_service
```

| Field | Meaning |
|---|---|
| `speed` | Divides travel time: `hours / speed` (1.25 = 20% faster than base) |
| `status` | `available` or `busy` or `out_of_service` |
| `mission` | `null`, or the in-flight mission below |

Mission object:

```
{"kind": "incident" | "move",
 "incident_id": "INC-012" | null,
 "origin": "LOC-1",       # where the vehicle came from (cancel returns here)
 "target": "LOC-5",
 "depart": 64,
 "arrival": 68}           # fixed at dispatch time — never recalculated
```

### 3.4 Incident

```
{"id": "INC-009", "title": "Ridge school bus rollover",
 "description": "...", "location_id": "LOC-6", "people_at_risk": 14,
 "deadline": 107, "status": "open",
 "assigned_vehicle_id": null,
 "created_at": 103, "resolved_at": null}
```

| `status` | Meaning |
|---|---|
| `open` | Clock has not reached `deadline` yet |
| `resolved` | A vehicle arrived by the deadline — `people_at_risk` saved |
| `missed` | Deadline passed with nobody there — `people_at_risk` lost |

`deadline = created_at + deadline_offset_hours` (the offset itself is never
revealed; the computed deadline is). At most one vehicle may be assigned to
an incident; a late arrival after the outcome is a wasted trip.

### 3.5 Chronic (care home)

```
{"name": "Sunrise Care Home", "location_id": "LOC-2",
 "interval_hours": 24, "loss_per_tick": 2, "initial_people": 24,
 "last_visit": 185, "people_remaining": 20, "next_tick": 216}
```

At every `next_tick` hour the engine checks: did **any** vehicle arrive at
`location_id` during `(tick − interval_hours, tick]` (inclusive)? If not,
`loss_per_tick` residents are lost (capped at `people_remaining`). Any
mission arrival counts — an incident response or a `/move` reposition.
Vehicles that simply **stay parked** there do not refresh the window; a
vehicle already at the care home must drive out and back (`/move` away,
then `/move` home) to count as a new visit.

### 3.6 Score

```
{"people_saved": 306,              # incident people saved so far
 "people_lost_incidents": 41,
 "people_lost_chronic": 0}
```

The headline number at the end is `people_saved_total` from `/report`
(incident saves + care residents still alive).

### 3.7 Log

Each entry: `{"hour", "day", "kind", "text"}`. `kind` is one of
`incident_open`, `incident_resolved`, `incident_missed`, `late_arrival`,
`dispatch`, `move`, `cancel`, `arrived`, `chronic_loss`, `chronic_ok`,
`road_status`, `vehicle_status`. `/advance` returns the slice of log lines
produced by that call under `"processed"` — a cheap way to react to what
just happened without diffing the whole state.

---

## 4. The three event types

Scenarios are scripts of timed events. **You never see events before they
fire** — this section shows what each one looks like once it has affected
`/state`. The raw authoring format (for scenario files) is the same JSON
under `events[].data`.

### 4.1 `incident` — a new emergency opens

```
{"time": 103, "type": "incident",
 "data": {"id": "INC-009", "title": "...", "description": "...",
          "location_id": "LOC-6", "people_at_risk": 14,
          "deadline_offset_hours": 4}}
```

Observed in `/state`: a new object appears in `incidents[]` with
`status: "open"`, `created_at = time`, `deadline = time + offset`, and the
log gains an `incident_open` line. Your move: decide whether any vehicle can
arrive `<= deadline`, and dispatch the best one.

### 4.2 `road_status` — a road changes condition

```
{"time": 300, "type": "road_status",
 "data": {"road_id": "RD-3", "status": "restricted",
          "reason": "wildfire smoke — single lane"}}
```

Observed: the matching `roads[]` entry's `status` flips and a `reason`
string appears; log gains a `road_status` line. Vehicles already en route
are unaffected; new dispatches recompute over the new graph (`closed` =
unreachable, `restricted` = 1.5×). A later event may set the road back to
`open`.

### 4.3 `vehicle_status` — a vehicle goes down or comes back

```
{"time": 800, "type": "vehicle_status",
 "data": {"vehicle_id": "VEH-02", "status": "out_of_service",
          "reason": "transmission failure"}}
```

Observed: the vehicle's `status` becomes `out_of_service` (with
`oos_reason`); if it was mid-mission the mission is **force-cancelled** —
it snaps back to its mission origin, the incident it was heading to becomes
unassigned again, and any travel time already spent is lost. A later
`"status": "available"` event brings it back at its current location.

---

## 5. Actions

### `POST /dispatch`

```json
{"vehicle_id": "VEH-02", "incident_id": "INC-009"}
```

Requires: vehicle `available`, incident `open` and unassigned, a route
exists. Returns:

```json
{"ok": true, "vehicle_id": "VEH-02", "incident_id": "INC-009",
 "arrival_hour": 107, "deadline_hour": 107,
 "arrives_before_deadline": true}
```

If `arrives_before_deadline` is false the engine adds a `warning`: nobody
will be saved **and the vehicle stays committed until it arrives**. Always
check feasibility yourself before dispatching.

### `POST /move`

```json
{"vehicle_id": "VEH-04", "location_id": "LOC-2"}
```

Repositions an available vehicle. Same feasibility rules as dispatch.
Moving to the location the vehicle is **already at** is an error — a real
trip out and back is required (this is how the care-home visit window gets
refreshed: move away, then move home).

### `POST /cancel`

```json
{"vehicle_id": "VEH-02"}
```

Aborts the mission: the vehicle reappears at its mission `origin`,
available, and the hours since `depart` are wasted. An incident that loses
its assigned vehicle becomes unassigned again. Cancelling is the only way to
redeploy a committed vehicle — trade the sunk hours against the new call.

### `POST /advance`

`{}` → process time until the next arrival / deadline / chronic tick /
scripted event. `{"to_hour": 500}` → process everything up to hour 500.
Returns `{"ok", "clock", "day", "processed": [log lines]}`.

### `POST /reset`

`{"scenario": "7day"}` restarts the engine from hour 0 (name is whitelisted
to letters/digits/`-`/`_`). Judges use this between runs; you can use it to
rehearse.

---

## 6. Simulation rules (the fine print)

1. **Integer hours.** Arrival = `dispatch_hour + ceil(shortest_route_hours /
   speed)`, computed once at dispatch. Restricted roads add 1.5× before the
   speed division; closed roads make the destination unreachable (action
   rejected with 400).
2. **All-or-nothing rescue.** Arrive at or before the deadline → every
   `people_at_risk` is saved. Miss it → all are lost when the deadline
   passes; the vehicle is still en route and still occupies the incident
   until it arrives (then the trip is logged as wasted).
3. **One vehicle per incident.** A second dispatch to the same incident is
   rejected.
4. **Same-hour order:** arrivals, then incident deadlines, then the chronic
   tick, then scripted events. So a vehicle that arrives exactly at the
   deadline hour still saves the incident, and a visit arriving exactly at
   the tick hour still covers the care window.
5. **Planning copies are safe.** You can rebuild a private `Simulation` from
   any `/state` snapshot (see `engine.sim.Simulation.from_public_state` in
   the shipped engine) to run what-if rollouts — the event queue starts
   empty, so rollouts can never leak the future.
6. **The clock only moves on `/advance`.** Think as long as you want at any
   hour.

---

## 7. Scoring

At the end of the run (`clock == horizon_hours`), `GET /report` gives:

| Field | Meaning |
|---|---|
| `people_saved` | Incident people rescued on time |
| `people_lost_incidents` | Incident people whose deadline passed |
| `people_lost_chronic` | Care residents lost to missed visits |
| `chronic_remaining` | Care residents still alive |
| `people_saved_total` | `people_saved + chronic_remaining` — the headline |
| `people_at_risk_total` | Everything that was ever at risk |
| `incidents_total/resolved/missed/open` | Per-incident counts |

The judging scenario **cannot** be solved for `people_saved_total =
people_at_risk_total`: some communities are beyond any vehicle's reach in
time. Maximise the total, defend the care home, and be ready to explain
every trade-off you made.

---

## 8. Strategy tips (from the reference planners)

- **Feasibility first.** Never dispatch a vehicle that arrives after the
  deadline — it saves nobody and removes the vehicle from play for the whole
  travel time. The `bad` reference does this and loses 25–35% of the score.
- **Earliest deadline first is a decent spine**, but watch for the call that
  only the fast ambulance can reach in time: giving the ambulance to
  care-home duty or to an earlier, smaller call can lose a bigger later one.
- **The care home is a metronome — and it is 8 hours out of town.** Someone
  must physically arrive at the care location in every `(tick − interval,
  tick]` window — every 24 hours, forever — or two residents die per missed
  window. A vehicle parked there is not enough; it has to leave and return.
  Budget a vehicle for this or watch the home empty out.
- **Cancel is a real option.** A vehicle committed to a 6-person call three
  hours into a 14-hour trip can be snapped back to its origin and sent to a
  14-person call instead — at the cost of the hours already spent.
- **Watch `/advance`'s `processed` log** rather than diffing whole states —
  arrivals, misses and status flips come back as a compact list.

Good luck.
