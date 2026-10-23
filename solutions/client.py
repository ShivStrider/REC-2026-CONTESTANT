"""Tiny stdlib HTTP client for the REC 2026 engine — shipped to teams.

This is the whole client surface: five GET/POST verbs plus a run_loop()
helper that shows the standard decide -> apply -> advance pattern. Copy it,
extend it, or write your own — the API is exactly these calls.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


class ClientError(Exception):
    """Server rejected a request (4xx/5xx) or was unreachable."""


class Client:
    def __init__(self, host: str = "127.0.0.1", port: int = 8600):
        self.base = f"http://{host}:{port}"

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = {}
            raise ClientError(body.get("error", f"HTTP {e.code}")) from None
        except OSError as e:
            raise ClientError(f"cannot reach engine: {e}") from None
        if not body.get("ok", False):
            raise ClientError(body.get("error", "request failed"))
        return body

    def health(self) -> dict:
        return self._request("GET", "/health")

    def state(self) -> dict:
        return self._request("GET", "/state")["state"]

    def report(self) -> dict:
        return self._request("GET", "/report")["report"]

    def dispatch(self, vehicle_id: str, incident_id: str) -> dict:
        return self._request("POST", "/dispatch",
                             {"vehicle_id": vehicle_id, "incident_id": incident_id})

    def move(self, vehicle_id: str, location_id: str) -> dict:
        return self._request("POST", "/move",
                             {"vehicle_id": vehicle_id, "location_id": location_id})

    def cancel(self, vehicle_id: str) -> dict:
        return self._request("POST", "/cancel", {"vehicle_id": vehicle_id})

    def advance(self, to_hour: int | None = None) -> dict:
        payload = {} if to_hour is None else {"to_hour": to_hour}
        return self._request("POST", "/advance", payload)

    def reset(self, scenario: str) -> dict:
        return self._request("POST", "/reset", {"scenario": scenario})

    def apply(self, action: dict) -> dict:
        kind = action.get("action")
        if kind == "dispatch":
            return self.dispatch(action["vehicle_id"], action["incident_id"])
        if kind == "move":
            return self.move(action["vehicle_id"], action["location_id"])
        if kind == "cancel":
            return self.cancel(action["vehicle_id"])
        raise ClientError(f"unknown action {kind!r}")


def run_loop(decide, host: str = "127.0.0.1", port: int = 8600,
             label: str = "planner") -> dict:
    """Drive one full scenario run with `decide(state) -> [actions]`.

    The standard loop: fetch state, ask decide() for this hour's actions,
    apply them all, then advance the engine to the next pending moment.
    Repeats until the horizon; returns the final /report.
    """
    client = Client(host, port)
    first = True
    while True:
        st = client.state()
        if st["clock"] >= st["horizon_hours"]:
            if first:
                print(f"[{label}] engine is already at the horizon — "
                      f"POST /reset before re-running", file=sys.stderr)
            break
        first = False
        for action in decide(st):
            try:
                client.apply(action)
            except ClientError as e:
                print(f"[{label}] rejected {action}: {e}", file=sys.stderr)
        out = client.advance()
        if out["clock"] == st["clock"]:
            print(f"[{label}] engine did not advance past hour "
                  f"{st['clock']}; stopping early", file=sys.stderr)
            break
    rep = client.report()
    day = min(rep["day"], rep["horizon_hours"] // 24)
    print(f"[{label}] day {day}/{rep['horizon_hours'] // 24} "
          f"saved {rep['people_saved_total']}/{rep['people_at_risk_total']} "
          f"(incidents {rep['people_saved']}, missed "
          f"{rep['people_lost_incidents']}, care home losses "
          f"{rep['people_lost_chronic']}, care residents still alive "
          f"{rep['chronic_remaining']})")
    return rep
