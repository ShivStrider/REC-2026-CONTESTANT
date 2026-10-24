"""Decision log: every planner decision with hour, action and reason.

Written two ways at once:
  - human-readable lines on stdout   H0052 d03 | DISPATCH  | VEH-AMB -> INC-006 | reason
  - machine-readable JSONL file      one object per line (for replay / reports)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# engine log kinds worth echoing as context between our decisions
ENGINE_EVENT_KINDS = {
    "incident_open", "incident_resolved", "incident_missed", "late_arrival",
    "road_status", "vehicle_status", "chronic_loss", "chronic_ok",
}


class DecisionLog:
    def __init__(self, path: Path | None = None, quiet: bool = False,
                 show_events: bool = True):
        self.quiet = quiet
        self.show_events = show_events
        self.file = None
        self.path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.file = open(path, "w", encoding="utf-8")
        self.counts: dict[str, int] = {}

    def _emit(self, record: dict, line: str) -> None:
        if self.file:
            self.file.write(json.dumps(record) + "\n")
            self.file.flush()
        if not self.quiet:
            try:
                print(line)
            except UnicodeEncodeError:
                print(line.encode("ascii", "replace").decode("ascii"))

    def decision(self, hour: int, action: str, vehicle: str | None,
                 target: str | None, reason: str, result: dict | None = None) -> None:
        self.counts[action] = self.counts.get(action, 0) + 1
        who = f"{vehicle} -> {target}" if vehicle and target else (vehicle or target or "")
        line = f"H{hour:04d} d{hour // 24 + 1:02d} | {action:<11} | {who:<20} | {reason}"
        self._emit({"type": "decision", "hour": hour, "action": action,
                    "vehicle": vehicle, "target": target, "reason": reason,
                    "result": result}, line)

    def event(self, entry: dict) -> None:
        if entry["kind"] not in ENGINE_EVENT_KINDS:
            return
        line = f"H{entry['hour']:04d} d{entry['day']:02d} |   engine    | {entry['text']}"
        record = {"type": "event", **entry}
        if self.show_events:
            self._emit(record, line)
        elif self.file:
            self.file.write(json.dumps(record) + "\n")

    def summary(self, report: dict, label: str) -> None:
        line = (f"[{label}] {report['scenario_id']} day {min(report['day'], report['horizon_hours'] // 24)}"
                f"/{report['horizon_hours'] // 24}  SAVED {report['people_saved_total']}"
                f"/{report['people_at_risk_total']}  (incidents saved {report['people_saved']}, "
                f"lost {report['people_lost_incidents']}; care home lost "
                f"{report['people_lost_chronic']}, alive {report['chronic_remaining']}; "
                f"incidents {report['incidents_resolved']}/{report['incidents_total']} resolved)")
        rec = {"type": "summary", "label": label,
               **{k: v for k, v in report.items() if k not in ("incidents", "chronic")}}
        if self.file:
            self.file.write(json.dumps(rec) + "\n")
        print(line, file=sys.stderr if self.quiet else sys.stdout)

    def close(self) -> None:
        if self.file:
            self.file.close()
            self.file = None
