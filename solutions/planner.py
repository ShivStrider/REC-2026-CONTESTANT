"""REC 2026 dispatch planner - command-line entry point and run loop.

    python -m solutions.planner --port 8600 --scenario 7day --profile full

Every setting can also come from the environment (REC_HOST, REC_PORT,
REC_SCENARIO, REC_PROFILE, REC_PACE), so nothing needs a code edit.

Loop per decision round:
    GET /state -> strategy.decide() -> POST each action -> POST /advance
Rejected actions (HTTP 4xx) are logged and skipped; the next round replans
from fresh state. The loop guarantees the clock always moves forward.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from .client import Client, ClientError
from .decision_log import DecisionLog
from .strategy import PROFILES, Action, Planner

ROOT = Path(__file__).resolve().parent.parent
DEMO_PACE = 2.0             # default seconds per step for the 7day scenario only


def _retry(fn, *args, attempts: int = 3):
    """Retry engine calls that fail because the engine is unreachable."""
    for i in range(attempts):
        try:
            return fn(*args)
        except ClientError as e:
            if "cannot reach engine" not in str(e) or i == attempts - 1:
                raise
            time.sleep(1.0 + i)


def _known_next_event(state: dict) -> int | None:
    """Next moment the engine already knows about (arrival, deadline, care
    tick). Scripted events are hidden, so this is only an upper bound on how
    far a plain /advance will jump."""
    clock = state["clock"]
    times = [v["mission"]["arrival"] for v in state["vehicles"] if v.get("mission")]
    times += [i["deadline"] for i in state["incidents"] if i["status"] == "open"]
    c = state.get("chronic")
    if c and c["people_remaining"] > 0:
        times.append(c["next_tick"])
    future = [t for t in times if t > clock]
    return min(future) if future else None


def apply_action(client, planner: Planner, log: DecisionLog, clock: int,
                 a: Action) -> dict | None:
    label = {"dispatch": "DISPATCH", "move": "MOVE", "cancel": "CANCEL"}[a.kind]
    if a.tag in ("care", "relay"):
        label = "CARE-" + ("RELAY" if a.tag == "relay" else "VISIT")
    elif a.tag == "reposition":
        label = "REPOSITION"
    try:
        if a.kind == "dispatch":
            res = client.dispatch(a.vehicle_id, a.target)
            if not res.get("arrives_before_deadline", True):
                # should never happen: we check feasibility first
                log.decision(clock, "WARNING", a.vehicle_id, a.target, res.get("warning", "late"))
        elif a.kind == "move":
            res = client.move(a.vehicle_id, a.target)
        else:
            res = client.cancel(a.vehicle_id)
    except ClientError as e:
        log.decision(clock, "REJECTED", a.vehicle_id, a.target,
                     f"{a.kind} refused by engine: {e} - will replan next round")
        planner.on_rejected(a)
        return None
    log.decision(clock, label, a.vehicle_id, a.target, a.reason, res)
    planner.on_applied(a, res)
    return res


def safe_decide(planner: Planner, fallback: Planner | None, log: DecisionLog,
                state: dict) -> list[Action]:
    """A bug in the strategy must never end the run. If decide() raises, log
    it and decide this round with the basic profile; if that fails too, take
    no action this round. The main profile is tried again next round."""
    clock = state["clock"]
    try:
        return planner.decide(state)
    except Exception as e:  # noqa: BLE001 - any strategy failure falls back
        planner.wakeup = None
        if fallback is None:
            log.decision(clock, "ERROR", None, None,
                         f"{planner.profile} strategy crashed ({type(e).__name__}: {e}); "
                         "taking no action this round")
            return []
        log.decision(clock, "ERROR", None, None,
                     f"{planner.profile} strategy crashed ({type(e).__name__}: {e}); "
                     f"deciding this round with {fallback.profile} instead")
    try:
        return fallback.decide(state)
    except Exception as e:  # noqa: BLE001
        log.decision(clock, "ERROR", None, None,
                     f"{fallback.profile} fallback also crashed ({type(e).__name__}: {e}); "
                     "taking no action this round")
        return []


def run(client, planner: Planner, log: DecisionLog, label: str = "planner",
        pace: float = 0.0) -> dict | None:
    """Play the scenario to the horizon. `pace` sleeps that many seconds after
    each advance so a live dashboard can follow the run (0 = full speed)."""
    state = _retry(client.state)
    if state["clock"] >= state["horizon_hours"]:
        print(f"[{label}] engine is already at the horizon (hour {state['clock']}); "
              f"re-run with --reset to restart the scenario", file=sys.stderr)
        return None
    seen = 0                                # engine log lines already echoed
    # used only for a round where the main strategy crashes
    fallback = Planner(PROFILES[0], log) if planner.profile != PROFILES[0] else None

    def echo_events(st: dict) -> int:
        for entry in st["log"][seen:]:
            log.event(entry)
        return len(st["log"])

    max_rounds = state["horizon_hours"] * 4 + 100
    for _ in range(max_rounds):
        seen = echo_events(state)
        clock = state["clock"]
        if clock >= state["horizon_hours"]:
            break
        # Decide and act. A trip of 0 hours (vehicle already on site) resolves
        # instantly and frees the vehicle in the same hour, so plan again
        # before letting the clock move.
        for _ in range(5):
            instant = False
            for action in safe_decide(planner, fallback, log, state):
                res = apply_action(client, planner, log, clock, action)
                if res and res.get("arrival_hour") == clock:
                    instant = True
            if not instant:
                break
            state = _retry(client.state)
            seen = echo_events(state)

        # Advance. A plain /advance jumps to the next arrival / deadline / tick /
        # scripted event. If we need control earlier (a planned care departure),
        # step one hour at a time so a hidden event can't slip past unanswered.
        wake = planner.wakeup
        nxt = _known_next_event(_retry(client.state)) if wake is not None else None
        if wake is not None and wake > clock and (nxt is None or wake < nxt):
            out = _retry(client.advance, clock + 1)
        else:
            out = _retry(client.advance)
        if out["clock"] <= clock:           # stall guard: force the clock forward
            out = _retry(client.advance, min(clock + 1, state["horizon_hours"]))
            if out["clock"] <= clock:
                log.decision(clock, "ERROR", None, None, "engine clock will not advance; stopping")
                break
        if pace > 0:
            time.sleep(pace)
        state = _retry(client.state)

    echo_events(state)
    report = _retry(client.report)
    log.summary(report, label)
    return report


def main(argv: list[str] | None = None) -> int:
    env = os.environ.get
    ap = argparse.ArgumentParser(description="REC 2026 dispatch planner")
    ap.add_argument("--host", default=env("REC_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(env("REC_PORT", "8600")))
    ap.add_argument("--scenario", default=env("REC_SCENARIO"),
                    help="reset the engine to this scenario before running (e.g. 7day, 90day)")
    ap.add_argument("--reset", action="store_true",
                    help="reset the engine (to --scenario, or its current scenario) first")
    ap.add_argument("--profile", choices=PROFILES, default=env("REC_PROFILE", PROFILES[-1]),
                    help=f"strategy profile (default: {PROFILES[-1]})")
    ap.add_argument("--log-dir", default=str(ROOT / "runs"),
                    help="where the JSONL decision log is written ('' to disable)")
    ap.add_argument("--pace", type=float,
                    default=float(env("REC_PACE")) if env("REC_PACE") else None,
                    help="seconds to pause after each advance, so the live dashboard "
                         f"can be watched (default {DEMO_PACE} for the 7day demo, 0 = full "
                         "speed for every other scenario)")
    ap.add_argument("--quiet", action="store_true", help="only print the final summary")
    ap.add_argument("--no-events", action="store_true",
                    help="hide engine events between decisions on stdout")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    client = Client(args.host, args.port)
    try:
        health = client.health()
        if args.scenario or args.reset:
            scenario = args.scenario or health["scenario"]
            client.reset(scenario)
            print(f"engine reset to scenario '{scenario}'", file=sys.stderr)
        scenario = client.state()["scenario_id"]
    except ClientError as e:
        print(f"error: {e} (is the engine running on {args.host}:{args.port}?)", file=sys.stderr)
        return 2

    path = None
    if args.log_dir:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = Path(args.log_dir) / f"{scenario}_{args.profile}_{stamp}.jsonl"
    log = DecisionLog(path, quiet=args.quiet, show_events=not args.no_events)
    planner = Planner(args.profile, log)
    # the 7-day run is the live demo, slowed so it can be explained; longer runs go full speed
    pace = args.pace if args.pace is not None else (DEMO_PACE if scenario == "7day" else 0.0)
    if pace > 0:
        print(f"pace {pace:g}s per step (--pace 0 for full speed)", file=sys.stderr)
    try:
        report = run(client, planner, log, label=f"{scenario}/{args.profile}",
                     pace=max(0.0, pace))
    except KeyboardInterrupt:
        print("\ninterrupted - engine left as is; re-run with --reset to start over",
              file=sys.stderr)
        return 130
    except ClientError as e:
        print(f"error: lost the engine: {e}", file=sys.stderr)
        return 2
    finally:
        log.close()
    if path:
        print(f"decision log: {path}", file=sys.stderr)
    return 0 if report else 1


if __name__ == "__main__":
    sys.exit(main())
