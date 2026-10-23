# REC 2026 — Emergency Response: 90 Days (starter kit)

Build a dispatch planner that drives four emergency vehicles for 90
simulated days over a local HTTP API. Python 3.10+ stdlib only.

## Quickstart

    python3 -m engine.server --scenario 7day --port 8600
    python3 -m solutions.[planner.py] --host 127.0.0.1 --port 8600

Loop: GET /state -> decide -> POST /dispatch | /move | /cancel -> POST /advance.

`solutions/client.py` is a working example HTTP client. The full API
reference — every endpoint, every data type, every event kind — is in
`docs/TEAM_GUIDE.md`. Read it before you code.

The engine never reveals future events. Incidents, road closures and
vehicle breakdowns appear in /state only when they happen.

People are saved only if a vehicle arrives before the incident deadline.
The care home loses two residents every 24 hours unless someone visits.
Good luck.
