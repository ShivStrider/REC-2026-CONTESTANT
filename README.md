# REC 2026 — Emergency Response: 90 Days

A dispatch planner that drives four emergency vehicles for 90 simulated days
over the engine's local HTTP API, plus the tools we use to test it and show
how it played. Python 3.10+ standard library only, no installs.

The engine never reveals future events. Incidents, road closures and vehicle
breakdowns appear in `/state` only when they happen. People are saved only if
a vehicle arrives by the incident deadline, and the care home loses two
residents every 24 hours unless a vehicle arrives there.

## Run it

Use `python3` instead of `python` on macOS and Linux.

1. Start the engine:

       python -m engine.server --scenario 7day --port 8600

2. In a second terminal, run the planner:

       python -m solutions.planner --port 8600 --scenario 7day

   `--scenario` resets the engine to that scenario first, so you can re-run
   without restarting the server. Every decision is printed with its reason
   and also saved to `runs/<scenario>_<profile>_<time>.jsonl`.

3. In a third terminal, open the dashboard to see the run:

       python -m dashboard.serve

   Then go to http://127.0.0.1:8700. It lists every log in `runs/`, newest
   first. You can also open `dashboard/index.html` straight from disk and
   drop a `.jsonl` file onto it.

For judging, use `--scenario 90day`. The API and the planner are the same.

### Planner options

| Option | Default | Meaning |
|---|---|---|
| `--host`, `--port` | `127.0.0.1`, `8600` | Where the engine is |
| `--scenario NAME` | none | Reset the engine to this scenario before running |
| `--reset` | off | Reset the engine to its current scenario |
| `--profile NAME` | the most complete one | Strategy profile; `--help` lists them |
| `--log-dir DIR` | `runs/` | Where the decision log goes (`''` turns it off) |
| `--quiet` | off | Print only the final summary |
| `--no-events` | off | Hide engine events between decisions |

Each option except the last three can also be set with an environment
variable: `REC_HOST`, `REC_PORT`, `REC_SCENARIO`, `REC_PROFILE`.

## Test it

    python -m tools.bench --scenarios 7day

The bench runs the planner in-process against the unmodified engine, with no
server, and prints one row per scenario and profile. Every row must say
`done yes`. With no arguments it runs `7day` plus every scenario file in
`tools/stress/`.

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
| `dashboard/` | Run dashboard: `index.html` and the `serve.py` that serves it |

## Reading the dashboard

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
