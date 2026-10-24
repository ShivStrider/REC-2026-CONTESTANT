"""Compare strategy profiles across scenarios, in-process (no server needed).

    python -m tools.bench                       # 7day + every tools/stress/*.json
    python -m tools.bench --scenarios 7day --profiles basic care full
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from solutions.decision_log import DecisionLog
from solutions.planner import run
from solutions.strategy import PROFILES, Planner
from tools.local_engine import LocalClient, load_scenario

STRESS = Path(__file__).resolve().parent / "stress"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", nargs="*",
                    default=["7day", *sorted(str(p) for p in STRESS.glob("*.json"))])
    ap.add_argument("--profiles", nargs="*", default=list(PROFILES))
    args = ap.parse_args()

    rows = []
    for sc in args.scenarios:
        scenario = load_scenario(sc)
        for prof in args.profiles:
            log = DecisionLog(None, quiet=True)
            t0 = time.perf_counter()
            rep = run(LocalClient(scenario), Planner(prof, log), log, label=prof)
            dt = time.perf_counter() - t0
            rows.append((scenario["scenario_id"], prof, rep, dt, log.counts.get("REJECTED", 0)))

    print(f"\n{'scenario':<10} {'profile':<6} {'saved':>9} {'incid':>6} {'lostInc':>7} "
          f"{'careLost':>8} {'resolved':>9} {'reject':>6} {'done':>5} {'sec':>6}")
    for sid, prof, r, dt, rej in rows:
        print(f"{sid:<10} {prof:<6} {r['people_saved_total']:>4}/{r['people_at_risk_total']:<4} "
              f"{r['people_saved']:>6} {r['people_lost_incidents']:>7} {r['people_lost_chronic']:>8} "
              f"{r['incidents_resolved']:>4}/{r['incidents_total']:<4} {rej:>6} "
              f"{'yes' if r['complete'] else 'NO':>5} {dt:>6.1f}")


if __name__ == "__main__":
    main()
