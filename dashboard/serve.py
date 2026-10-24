"""Serve the run dashboard over HTTP (stdlib only).

    python -m dashboard.serve                 # http://127.0.0.1:8700
    python -m dashboard.serve --port 8701 --runs runs

Endpoints:
    GET /                    dashboard/index.html
    GET /api/runs            JSON list of *.jsonl decision logs, newest first
    GET /runs/<name>.jsonl   one decision log
    GET /scenario/<id>       scenarios/<id>.json (names for locations and vehicles)

The page also works opened straight from disk: drop a .jsonl file onto it.
"""
from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = Path(__file__).resolve().parent / "index.html"
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")

RUNS_DIR: Path = ROOT / "runs"


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
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._file(PAGE, "text/html; charset=utf-8")
        elif path == "/api/runs":
            runs = []
            if RUNS_DIR.is_dir():
                for p in RUNS_DIR.glob("*.jsonl"):
                    st = p.stat()
                    runs.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
            runs.sort(key=lambda r: r["mtime"], reverse=True)
            self._json(200, {"ok": True, "dir": str(RUNS_DIR), "runs": runs})
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


def main() -> None:
    global RUNS_DIR
    ap = argparse.ArgumentParser(description="REC 2026 run dashboard")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8700)
    ap.add_argument("--runs", default=str(ROOT / "runs"),
                    help="folder of decision logs (default: runs/, where the planner writes)")
    args = ap.parse_args()
    RUNS_DIR = Path(args.runs).resolve()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"dashboard on http://{args.host}:{args.port}  (runs from {RUNS_DIR})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        server.server_close()


if __name__ == "__main__":
    main()
