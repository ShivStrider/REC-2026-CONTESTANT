"""HTTP API for the REC 2026 simulation engine.

Run:
    python3 -m engine.server --scenario scenarios/7day.json --port 8600

Endpoints (all JSON):
    GET  /health            liveness
    GET  /state             full current state (no future events)
    GET  /report            end-of-run style summary (works mid-run too)
    POST /dispatch          {"vehicle_id", "incident_id"}
    POST /move              {"vehicle_id", "location_id"}
    POST /cancel            {"vehicle_id"}
    POST /advance           {"to_hour": int?}   default = next pending event
    POST /reset             {"scenario": "7day"} (whitelisted name, no path chars)

A single global lock serialises all access; the engine clock only moves
when a client asks it to. CORS is wide open (localhost judging tool).
"""
from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .sim import Simulation, SimulationError

SCENARIO_DIR = Path(__file__).resolve().parent.parent / "scenarios"
SCENARIO_NAME = re.compile(r"^[A-Za-z0-9_-]+$")

_lock = threading.Lock()
_sim: Simulation = None  # set in main()
_default_scenario: str = ""


def _load_scenario(name: str) -> dict:
    if not SCENARIO_NAME.match(name):
        raise SimulationError(f"bad scenario name '{name}'")
    path = SCENARIO_DIR / f"{name}.json"
    if not path.is_file():
        raise SimulationError(f"unknown scenario '{name}'")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Handler(BaseHTTPRequestHandler):
    server_version = "REC2026/1.0"

    # -- plumbing -----------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, message: str) -> None:
        self._send(code, {"ok": False, "error": message})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise SimulationError("request body must be JSON")
        if not isinstance(data, dict):
            raise SimulationError("request body must be a JSON object")
        return data

    # -- verbs --------------------------------------------------------------

    def do_OPTIONS(self) -> None:
        self._send(200, {"ok": True})

    def do_GET(self) -> None:
        try:
            path = self.path.split("?", 1)[0]
            with _lock:
                if path == "/health":
                    self._send(200, {"ok": True, "clock": _sim.clock,
                                     "scenario": _sim.scenario_id})
                elif path == "/state":
                    self._send(200, {"ok": True, "state": _sim.state()})
                elif path == "/report":
                    self._send(200, {"ok": True, "report": _sim.report()})
                else:
                    self._error(404, f"no such endpoint {path}")
        except SimulationError as e:
            self._error(400, str(e))
        except Exception as e:  # noqa: BLE001 — never leak a traceback to clients
            self._error(500, f"internal error: {e}")

    def do_POST(self) -> None:
        global _sim
        try:
            path = self.path.split("?", 1)[0]
            body = self._body()
            with _lock:
                if path == "/dispatch":
                    result = _sim.dispatch(body.get("vehicle_id"),
                                           body.get("incident_id"))
                elif path == "/move":
                    result = _sim.move(body.get("vehicle_id"),
                                       body.get("location_id"))
                elif path == "/cancel":
                    result = _sim.cancel(body.get("vehicle_id"))
                elif path == "/advance":
                    result = _sim.advance(body.get("to_hour"))
                elif path == "/reset":
                    _sim = Simulation(_load_scenario(
                        body.get("scenario", _default_scenario)))
                    result = {"ok": True, "reset_to": _sim.scenario_id,
                              "clock": _sim.clock}
                else:
                    self._error(404, f"no such endpoint {path}")
                    return
            self._send(200, result)
        except SimulationError as e:
            self._error(400, str(e))
        except Exception as e:  # noqa: BLE001
            self._error(500, f"internal error: {e}")


def main() -> None:
    global _sim, _default_scenario
    ap = argparse.ArgumentParser(description="REC 2026 simulation server")
    ap.add_argument("--scenario", default="7day",
                    help="scenario name under scenarios/ (default: 7day)")
    ap.add_argument("--port", type=int, default=8600)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    _default_scenario = args.scenario
    _sim = Simulation(_load_scenario(args.scenario))

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"REC 2026 engine: scenario '{_sim.scenario_id}' "
          f"({_sim.horizon}h) on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        server.server_close()


if __name__ == "__main__":
    main()
