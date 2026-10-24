"""Serve the dashboard over HTTP (stdlib only).

    python -m dashboard                                   # http://127.0.0.1:8700, engine on :8600
    python -m dashboard --engine-port 8600 --port 8700 --runs runs

The page has two views:
    Live    polls the engine's GET /state and GET /report once a second
            (the engine allows cross-origin requests) and shows the newest
            planner decisions from runs/*.jsonl as they are written.
    Replay  any finished decision log: timeline, incidents, full feed.

Endpoints:
    GET /  (or /map)              dashboard/map.html: live map of roads, vehicles, incidents, care home
    GET /numbers                  dashboard/index.html: live numbers and run replay
    GET /api/config               {"engine": "http://host:port"} for the live view
    GET /api/runs                 *.jsonl decision logs, newest first
    GET /decisions?file=F&since=N log records from line N of the newest log
                                  (from line 0 when a newer log has started)
    GET /runs/<name>.jsonl        one decision log
    GET /scenario/<id>            scenarios/<id>.json (names for locations and vehicles)

The page also works opened straight from disk: drop a .jsonl file onto it.
"""
from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
PAGE = Path(__file__).resolve().parent / "index.html"
MAP_PAGE = Path(__file__).resolve().parent / "map.html"
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
MAX_RECORDS = 2000                  # per /decisions reply; the page asks again for the rest

RUNS_DIR: Path = ROOT / "runs"
ENGINE_URL = "http://127.0.0.1:8600"


def newest_run() -> Path | None:
    if not RUNS_DIR.is_dir():
        return None
    runs = list(RUNS_DIR.glob("*.jsonl"))
    return max(runs, key=lambda p: p.stat().st_mtime) if runs else None


def read_records(path: Path, since: int) -> tuple[list[dict], int]:
    """Complete JSON lines from line `since` on, and the next line to ask for.
    A half-written last line is left for the next poll."""
    records: list[dict] = []
    line_no = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line_no < since:
                line_no += 1
                continue
            if not line.endswith("\n") or len(records) >= MAX_RECORDS:
                break
            line_no += 1
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records, line_no


class Handler(BaseHTTPRequestHandler):
    server_version = "REC2026-dashboard/1.0"

    def log_message(self, fmt: str, *args) -> None:
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _file(self, path: Path, ctype: str) -> None:
        if not path.is_file():
            self._json(404, {"ok": False, "error": f"not found: {path.name}"})
            return
        self._send(200, path.read_bytes(), ctype)

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        path, query = url.path, parse_qs(url.query)
        if path in ("/", "/map", "/map.html"):
            self._file(MAP_PAGE, "text/html; charset=utf-8")
        elif path in ("/numbers", "/index.html"):
            self._file(PAGE, "text/html; charset=utf-8")
        elif path == "/api/config":
            self._json(200, {"ok": True, "engine": ENGINE_URL, "runs_dir": str(RUNS_DIR)})
        elif path == "/api/runs":
            runs = []
            if RUNS_DIR.is_dir():
                for p in RUNS_DIR.glob("*.jsonl"):
                    st = p.stat()
                    runs.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
            runs.sort(key=lambda r: r["mtime"], reverse=True)
            self._json(200, {"ok": True, "dir": str(RUNS_DIR), "runs": runs})
        elif path == "/decisions":
            self._decisions(query)
        elif path.startswith("/runs/"):
            name = path[len("/runs/"):]
            if not SAFE_NAME.match(name) or not name.endswith(".jsonl"):
                self._json(400, {"ok": False, "error": "bad run name"})
                return
            self._file(RUNS_DIR / name, "application/x-ndjson; charset=utf-8")
        elif path.startswith("/scenario/"):
            sid = path[len("/scenario/"):]
            if not SAFE_NAME.match(sid) or "." in sid:
                self._json(400, {"ok": False, "error": "bad scenario id"})
                return
            self._file(ROOT / "scenarios" / f"{sid}.json", "application/json; charset=utf-8")
        else:
            self._json(404, {"ok": False, "error": f"no such endpoint {path}"})

    def _decisions(self, query: dict) -> None:
        run = newest_run()
        if run is None:
            self._json(200, {"ok": True, "file": None, "next": 0, "reset": False, "records": []})
            return
        asked = query.get("file", [""])[0]
        try:
            since = max(0, int(query.get("since", ["0"])[0]))
        except ValueError:
            since = 0
        reset = asked != run.name
        if reset:
            since = 0
        records, nxt = read_records(run, since)
        self._json(200, {"ok": True, "file": run.name, "next": nxt, "reset": reset, "records": records})


def main() -> None:
    global RUNS_DIR, ENGINE_URL
    ap = argparse.ArgumentParser(description="REC 2026 dashboard")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8700)
    ap.add_argument("--engine-host", default="127.0.0.1")
    ap.add_argument("--engine-port", type=int, default=8600)
    ap.add_argument("--runs", default=str(ROOT / "runs"),
                    help="folder of decision logs (default: runs/, where the planner writes)")
    args = ap.parse_args()
    RUNS_DIR = Path(args.runs).resolve()
    ENGINE_URL = f"http://{args.engine_host}:{args.engine_port}"

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"dashboard on http://{args.host}:{args.port}  "
          f"(engine {ENGINE_URL}, logs from {RUNS_DIR})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        server.server_close()


if __name__ == "__main__":
    main()
