"""Generate synthetic 90-day stress scenarios (the real judging file is only
released at competition time). Written to tools/stress/, never scenarios/.

Same rules and shape as 7day.json, scaled up: 4 vehicles, a care home 8h out,
a remote community no one can reach in time, road closures and breakdowns
that land mid-mission.

    python -m tools.make_stress --seeds 1 2 3 4 5
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

OUT = Path(__file__).resolve().parent / "stress"
HORIZON = 2160

LOCATIONS = [
    ("LOC-1", "Community Hall", "town"),
    ("LOC-2", "Sunrise Care Home", "care"),
    ("LOC-3", "Mobile Home Park", "residential"),
    ("LOC-4", "Highway Depot", "depot"),
    ("LOC-5", "Riverside Cabins", "residential"),
    ("LOC-6", "East Ridge", "rural"),
    ("LOC-7", "Lakeshore Dock", "dock"),
    ("LOC-8", "Prairie Junction", "rural"),
    ("LOC-9", "Far North Reserve", "rural"),
]
ROADS = [
    ("RD-1", "LOC-1", "LOC-2", 8), ("RD-2", "LOC-4", "LOC-1", 3),
    ("RD-3", "LOC-1", "LOC-6", 5), ("RD-4", "LOC-1", "LOC-3", 2),
    ("RD-5", "LOC-3", "LOC-5", 3), ("RD-6", "LOC-2", "LOC-6", 8),
    ("RD-7", "LOC-2", "LOC-5", 8), ("RD-8", "LOC-4", "LOC-6", 6),
    ("RD-9", "LOC-1", "LOC-7", 4), ("RD-10", "LOC-7", "LOC-8", 5),
    ("RD-11", "LOC-4", "LOC-8", 4), ("RD-12", "LOC-8", "LOC-9", 12),
]
# where incidents happen (weights) - the remote reserve is rare but deadly
SITES = {"LOC-1": 3, "LOC-2": 1, "LOC-3": 4, "LOC-4": 1, "LOC-5": 4,
         "LOC-6": 3, "LOC-7": 3, "LOC-8": 2, "LOC-9": 1}


def build(seed: int) -> dict:
    rnd = random.Random(seed)
    events = []
    t, n = 0, 0
    while True:
        t += rnd.choice([2, 4, 6, 8, 10, 12, 14, 16])
        if t >= HORIZON - 20:
            break
        for _ in range(rnd.choice([1, 1, 1, 2, 2, 3])):
            n += 1
            loc = rnd.choices(list(SITES), weights=list(SITES.values()))[0]
            offset = rnd.randint(3, 18)
            events.append({"time": t, "type": "incident", "data": {
                "id": f"INC-{n:03d}", "title": f"Synthetic incident {n}",
                "description": "generated", "location_id": loc,
                "people_at_risk": rnd.randint(2, 15), "deadline_offset_hours": offset}})
    for _ in range(18):                     # road trouble, later repaired
        road = rnd.choice(ROADS)[0]
        start = rnd.randint(10, HORIZON - 100)
        status = rnd.choice(["restricted", "restricted", "closed"])
        events.append({"time": start, "type": "road_status",
                       "data": {"road_id": road, "status": status, "reason": "storm damage"}})
        events.append({"time": start + rnd.randint(12, 72), "type": "road_status",
                       "data": {"road_id": road, "status": "open", "reason": "repaired"}})
    vehicles = ["VEH-AMB", "VEH-RES", "VEH-SUP", "VEH-ENG"]
    for _ in range(8):                      # breakdowns, often mid-mission
        vid = rnd.choice(vehicles)
        start = rnd.randint(10, HORIZON - 100)
        events.append({"time": start, "type": "vehicle_status",
                       "data": {"vehicle_id": vid, "status": "out_of_service",
                                "reason": "mechanical failure"}})
        events.append({"time": start + rnd.randint(8, 48), "type": "vehicle_status",
                       "data": {"vehicle_id": vid, "status": "available",
                                "reason": "repaired"}})
    events.sort(key=lambda e: e["time"])
    return {
        "scenario_id": f"stress{seed}",
        "title": f"Synthetic 90-day stress test (seed {seed})",
        "narrative": "Generated for offline testing only.",
        "horizon_hours": HORIZON,
        "locations": [{"id": i, "name": nm, "kind": k} for i, nm, k in LOCATIONS],
        "roads": [{"id": i, "name": i, "from_location_id": a, "to_location_id": b,
                   "hours": h, "status": "open"} for i, a, b, h in ROADS],
        "vehicles": [
            {"id": "VEH-AMB", "name": "Ambulance", "speed": 1.25, "location_id": "LOC-1"},
            {"id": "VEH-RES", "name": "Rescue Truck", "speed": 1.0, "location_id": "LOC-4"},
            {"id": "VEH-SUP", "name": "Supply Van", "speed": 1.0, "location_id": "LOC-4"},
            {"id": "VEH-ENG", "name": "Fire Engine", "speed": 1.0, "location_id": "LOC-1"},
        ],
        "chronic": {"name": "Sunrise Care Home", "location_id": "LOC-2",
                    "interval_hours": 24, "loss_per_tick": 2, "initial_people": 60},
        "events": events,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3, 4, 5])
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    for s in args.seeds:
        path = OUT / f"stress{s}.json"
        path.write_text(json.dumps(build(s), indent=1), encoding="utf-8")
        print(path)


if __name__ == "__main__":
    main()
