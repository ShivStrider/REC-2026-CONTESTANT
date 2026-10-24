# REC 2026 — Emergency Response: 90 Days

A dispatch planner that drives four emergency vehicles for 90 simulated days
over the engine's local HTTP API, plus the tools we use to test it and show
how it played. Python 3.10+ standard library only, no installs.

The engine never reveals future events. Incidents, road closures and vehicle
breakdowns appear in `/state` only when they happen. People are saved only if
a vehicle arrives by the incident deadline, and the care home loses two
residents every 24 hours unless a vehicle arrives there.

## Requirements

Python 3.10 or newer. Nothing to install: the engine, the planner, the bench
and the dashboard use only the standard library.

## Run it

Use `python3` instead of `python` on macOS and Linux.

1. Start the engine:

       python -m engine.server --scenario 7day --port 8600

2. In a second terminal, run the planner:

       python -m solutions.planner --port 8600 --scenario 7day --profile full

   Every decision is printed with its reason and also saved to
   `runs/<scenario>_<profile>_<time>.jsonl`.

3. In a third terminal, start the dashboard and open http://127.0.0.1:8700:

       python -m dashboard --engine-port 8600

   Start it before the planner to watch the run as it happens. The 7-day
   run is slowed to 2 seconds a step by default so it can be followed.
   The front page is the live map. The
   **Numbers** button (http://127.0.0.1:8700/numbers) opens the live
   numbers and the **Replay a run** tab, which opens any finished log in
   `runs/`. You can also open `dashboard/index.html` straight from disk and
   drop a `.jsonl` file onto it.

For judging, use `--scenario 90day`. The API and the planner are the same.

### Running again

The engine keeps its state between runs. If it has already reached the end
of the scenario, the planner stops with "engine is already at the horizon"
rather than doing nothing silently. To start over without restarting the
server, pass `--scenario 7day` (reset to that scenario) or `--reset` (reset
to whatever scenario the engine is running).

### Strategy profiles

Each profile builds on the one before it. Pick one with `--profile`; the
default is `full`.

| Profile | 7-day saved | What it adds |
|---|---|---|
| `basic` | 88 / 108 | Earliest deadline first. Sends the vehicle that arrives soonest, and never one that would arrive after the deadline. |
| `care` | 88 / 108 | Care-home visits timed to land exactly on the tick hour, an out-and-back relay when a vehicle is parked there, and recovery after breakdowns. No care-home residents lost. |
| `full` | 108 / 108 | Searches for the assignment that saves the most people, cancels a trip only when that saves more, repositions idle vehicles, and replans in the same hour after instant arrivals. |

### Reading a log line

    H0052 d03 | DISPATCH    | VEH-AMB -> INC-006   | EDF: 10 people at LOC-6 East Ridge, arrive H42 <= deadline H42 ...

From left to right: the hour (`H0052`) and day (`d03`), the action, the
vehicle and its target (an incident or a location), and why the planner
chose it. Lines marked `engine` are what the engine reported in between,
such as an incident opening or a care-home tick. `REJECTED` means the
engine refused an action; the planner logs it and replans next round.
`ERROR` means the strategy itself crashed in that round. The planner then
decides that round with `basic` instead and tries the chosen profile again
next round, so a bug never stops a run before the end of the scenario.

### Planner options

| Option | Default | Meaning |
|---|---|---|
| `--host`, `--port` | `127.0.0.1`, `8600` | Where the engine is |
| `--scenario NAME` | none | Reset the engine to this scenario before running |
| `--reset` | off | Reset the engine to its current scenario |
| `--profile NAME` | the most complete one | Strategy profile; `--help` lists them |
| `--pace SECONDS` | `2` for `7day`, `0` otherwise | Pause after each step so the live dashboard can be followed. The 7-day run is the demo, so it defaults to 2 seconds a step (about 2½ minutes in all); 90-day and other runs go at full speed. `--pace 0` runs the 7-day scenario at full speed |
| `--log-dir DIR` | `runs/` | Where the decision log goes (`''` turns it off) |
| `--quiet` | off | Print only the final summary |
| `--no-events` | off | Hide engine events between decisions |

These can also be set with environment variables: `REC_HOST`, `REC_PORT`,
`REC_SCENARIO`, `REC_PROFILE`, `REC_PACE`.

## Test it

    python -m tools.bench --scenarios 7day

The bench runs the planner in-process against the unmodified engine, with no
server, and prints one row per scenario and profile. Every row must say
`done yes`. With no arguments it runs `7day` plus every scenario file in
`tools/stress/`.

The unit tests need no server either:

    python -m unittest discover tests

To make practice 90-day scenarios (the real one is only released at the
competition), run `python -m tools.make_stress --seeds 1 2 3 4 5`. They are
written to `tools/stress/`, never to `scenarios/`.

## How the planner decides

This describes the `full` profile, the one we submit. `basic` and `care`
are earlier steps kept for comparison.

### Prioritisation

Every round, the planner looks at all open incidents, the next care-home
tick and every vehicle, and searches for the assignment that saves the most
people. The search is exhaustive branch-and-bound over which vehicle takes
which task and in what order. It includes vehicles that are still on a
trip (from where and when they will be free) and two-stop chains, such as
an incident and then the care home. Ties go to the plan with the least
total driving.

- **People first.** The score of a plan is people saved. Deadlines only
  order the search; a later, bigger incident can win over an earlier,
  smaller one.
- **Feasibility check.** A vehicle is only ever sent if it arrives by the
  deadline: arrival = now + ceil(route hours / speed), with restricted roads
  at ×1.5 and closed roads removed, the same sums the engine does. A late
  vehicle saves nobody and is stuck until it arrives, so the planner never
  sends one (`UNREACHABLE`).
- **Search size.** Each round searches the 9 earliest-deadline tasks, with
  a cap of 150,000 search steps. Anything beyond that is reconsidered next
  round (`DEFERRED`).
- **Repositioning.** A vehicle with nothing to do moves to where it cuts the
  *expected people missed* if new incidents look like the ones seen so far.
  Each location is weighted by the people seen there, and a response of h
  hours counts as a miss for the share of past deadlines shorter than h.
  It only uses incidents that have already happened. A move needs to cut
  the expected misses by at least 10% and take at most 10 hours. A
  repositioning trip can be cancelled at no cost in people when an
  incident needs the vehicle.

### Care home and incidents

The care home loses 2 residents at every tick hour T unless a vehicle
arrived during [T − 24, T]. In the engine code both ends are inclusive, so
a visit that arrives exactly on tick T covers tick T and tick T + 24. The
planner times every visit to land exactly on a tick hour: one trip every
48 hours instead of every 24.

- **Leave as late as possible.** The care vehicle departs at the last hour
  that still gets it there on the tick, so it stays free for incidents
  until then (`HOLD`).
- **Relay.** A vehicle parked at the home does not count as a visit, so it
  drives out to the nearest location and back, arriving on the tick
  (`CARE-RELAY`).
- **Incident or care visit.** The care visit is one more task in the
  search, worth the 2 residents at stake. An incident takes the care
  vehicle only if that saves more people than the care tick would cost. A
  dispatch to an incident at the care home also counts as the visit.
- If no vehicle can reach the home in time without giving up incidents that
  save more, the planner says so (`CARE-RISK`).

### Cancel rule

Each round the planner also asks, for every vehicle already on a trip:
what if it turned back now? A cancelled vehicle restarts from where its
trip began, at the current hour, so the hours already driven are lost.
That cost is part of the comparison. The planner cancels only if the
replanned schedule saves strictly more people than keeping the trip
(`CANCEL`). When an incident goes unserved and a cancel was considered and
rejected, the log says why (`KEEP`).

### Breakdowns and road changes

When a vehicle breaks down mid-trip, the engine cancels its mission and
puts it back where the trip started. The planner notices that its incident
is unassigned again and replans in the same round (`RECOVER`). Road
closures and restrictions change the travel times for every new search;
trips already under way keep their arrival hour, as the engine does.

## Log actions

| Action | Meaning |
|---|---|
| `DISPATCH` | Sends a vehicle to an incident. The reason gives the arrival hour, the deadline, the slack and which other vehicles could have made it. |
| `CARE-VISIT` | Sends a vehicle to the care home to arrive exactly on a tick hour. |
| `CARE-RELAY` | A vehicle parked at the home drives out and back, because parking does not count as a visit. |
| `REPOSITION` | Moves an idle vehicle to cut the expected people missed. |
| `MOVE` | Any other move. |
| `CANCEL` | Turns a vehicle back because the replanned schedule saves strictly more people. |
| `KEEP` | A cancel was considered and rejected; the reason gives both totals. |
| `HOLD` | A vehicle is reserved for a later care tick and waits for its departure hour. |
| `RECOVER` | A breakdown cancelled a trip; its incident is open again and is replanned now. |
| `WAIT` | No available vehicle can reach this incident in time yet (`basic` and `care` profiles). |
| `SKIP` | The only vehicle that could make it is needed for the care tick, and the tick is worth more (`care` profile). |
| `DEFERRED` | Outside the 9 earliest-deadline tasks searched this round; reconsidered next round. |
| `UNREACHABLE` | No vehicle can arrive by the deadline, so none is sent. |
| `TRADE-OFF` | Reachable, but the vehicles that could reach it save more people elsewhere. |
| `CARE-RISK` | The next care tick can't be covered without giving up incidents that save more. |
| `REJECTED` | The engine refused an action. Logged and replanned next round. |
| `WARNING` | The engine reports a dispatch that arrives after the deadline. The feasibility check should prevent this. |
| `ERROR` | The strategy crashed this round. That round falls back to `basic`, and the next round tries `full` again. |

## Results

People saved out of everyone at risk, care-home residents included.

| Scenario | `basic` | `care` | `full` |
|---|---|---|---|
| `7day` | 88 / 108 | 88 / 108 | 108 / 108 |
| `stress1` (90 days) | 3255 / 3615 | 3177 / 3615 | 3432 / 3615 |
| `stress2` (90 days) | 2938 / 3235 | 2887 / 3235 | 3103 / 3235 |
| `stress3` (90 days) | 2962 / 3213 | 2938 / 3213 | 3130 / 3213 |
| `stress4` (90 days) | 3173 / 3532 | 3178 / 3532 | 3380 / 3532 |
| `stress5` (90 days) | 2902 / 3236 | 2898 / 3236 | 3037 / 3236 |

On every practice scenario `full` saves 135 to 207 more people than
`basic`, with no rejected actions. `basic` loses all 60 care-home residents
on each 90-day run; `full` loses between 0 and 6. `care` loses at most 4
residents but misses more incidents than `basic`, because it guards the
care vehicle without searching for a better overall plan.

The practice scenarios come from `tools/make_stress.py` (seeds 1 to 5): 90
days, 4 vehicles, a care home 8 hours out, breakdowns, road closures and
some incidents no vehicle can reach in time. They are our own guesses at the
judging scenario, not the real one.

## Limitations

- **No knowledge of the future.** The planner only sees incidents once they
  open. Repositioning learns from past incidents, so early in a run it
  relies on even guesses for every location.
- **Search limits.** Each round searches the 9 earliest-deadline tasks and
  at most 150,000 steps. With many incidents open at once, a later one can
  wait a round (`DEFERRED`).
- **No turning mid-trip.** The engine fixes a trip's arrival at dispatch.
  The only way to redirect a vehicle is to cancel, which sends it back to
  where the trip started.
- **One care tick at a time.** The planner plans the next uncovered tick,
  not a whole schedule of visits ahead.
- **Tested on our own scenarios.** The 90-day results above come from
  generated scenarios; the real one may differ.

## What's where

| Path | What it is |
|---|---|
| `engine/` | The competition engine, unmodified |
| `scenarios/` | Scenario files (`7day.json` for training) |
| `docs/TEAM_GUIDE.md` | The full API reference from the organisers |
| `solutions/planner.py` | Entry point and run loop: state, decide, act, advance |
| `solutions/strategy.py` | The dispatch strategy; turns one `/state` into actions |
| `solutions/roads.py` | Travel times, computed the same way the engine does |
| `solutions/decision_log.py` | Writes every decision with its reason, to screen and JSONL |
| `solutions/client.py` | The organisers' HTTP client |
| `tools/bench.py` | Offline comparison of profiles across scenarios |
| `tools/local_engine.py` | In-process stand-in for the HTTP engine |
| `dashboard/` | Live and replay dashboard (`index.html`), live map (`map.html`), and the `serve.py` that serves them |

## Reading the dashboard

Dashboard options: `--port` (default 8700), `--engine-host` and
`--engine-port` (where the engine is, default 127.0.0.1:8600), and `--runs`
(the log folder, default `runs/`).

### Live

- **Clock:** day and hour, with progress through the scenario.
- **Score so far:** people saved out of everyone at risk so far, incident
  saves and misses, care-home residents alive, and open incidents.
- **Vehicles:** status, where each one is, and for a vehicle on a trip,
  where it is going and when it arrives.
- **Care home:** residents left, the last visit, the next tick, and whether
  the current window is covered, has a vehicle on the way, or still needs
  one.
- **Open incidents:** most urgent first, with hours left before the deadline
  and the vehicle responding.
- **Latest planner decisions:** the last 30 decisions with their reasons,
  plus missed deadlines and care-home losses.

### Live map

The front page, http://127.0.0.1:8700.

- **Roads:** the scenario has no coordinates, so locations are placed so
  that distance on screen follows travel hours. Each road shows its hours.
  Restricted roads are dashed and marked ×1.5; closed roads are red with
  an ✕.
- **Route search:** every new trip replays the shortest-route search the
  planner ran. A wave spreads out from the vehicle along open roads in
  order of travel time, then the route it chose lights up. Closed roads
  never light up, so a detour around a closure is visible.
- **Vehicles:** dots that move along their actual route, coloured by job:
  blue to an incident, purple for a care-home trip, grey for
  repositioning. A cancelled trip, or a breakdown, shows as a flash where
  the trip started, which is where the engine puts the vehicle back. A
  vehicle out of service is drawn as a red ✕.
- **Incidents:** a pulsing ring that shrinks as the deadline gets closer,
  red while no vehicle is assigned and blue once one is on the way. A save
  bursts green; a miss bursts grey.
- **Care home:** a 24-hour dial that drains as the last visit's cover runs
  out and refills when a vehicle arrives. The black mark is the next tick.
- **Side panel:** the score so far, where each vehicle is going and when it
  gets there, and the open incidents with hours left.
- **Subtitles:** the planner's last two decisions and their reasons.

### Replay a run

- **Summary tiles:** people saved out of everyone at risk (the headline
  score), incident saves, care-home residents still alive, and how many
  actions the engine rejected.
- **Vehicle timeline:** one row per vehicle. Each bar is a trip from
  departure to arrival: blue for incident responses, purple for care-home
  trips, grey for repositioning. Red bands are out-of-service periods. A
  faded bar with a dark mark was cancelled or cut short by a breakdown. The
  incident row shows each incident's open window, green if saved and red if
  missed. The care row shows each 24-hour tick.
- **Saved and lost over time:** running totals across the run.
- **Incidents:** every incident with its responder, arrival hour and the
  slack left before the deadline.
- **Decision feed:** every decision and engine event in order. Filter by
  type or search for an incident or vehicle id. Clicking a bar, an incident
  or a decision type filters the feed to it.

## Decision log format

One JSON object per line:

- `{"type": "decision", "hour", "action", "vehicle", "target", "reason", "result"}`.
  `action` is for example `DISPATCH`, `MOVE`, `CANCEL`, `WAIT` or `REJECTED`.
  `result` is the engine's reply, for example `arrival_hour` and
  `deadline_hour` for a dispatch.
- `{"type": "event", "hour", "day", "kind", "text"}`, an engine log line
  such as `incident_open`, `incident_resolved` or `chronic_loss`.
- `{"type": "summary", ...}`, the final `/report` without the per-incident
  list. It is the last line of a finished run.

## Use of AI tools

We used Claude, Anthropic's AI assistant, through Claude Code while building
this project. It helped write and review the planner, the tests, the
dashboard and this README. Every change was checked against the bench and
the unit tests before it was merged, and the team can explain how each part
works. The competition engine in `engine/` and the scenario files in
`scenarios/` are the organisers' and are unmodified.
