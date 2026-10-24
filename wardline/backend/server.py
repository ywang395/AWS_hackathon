"""
backend/server.py -- Wardline backend as ONE FastAPI process (CLAUDE.md section 3).

The workshop account cannot run Lambda (no IAM role creation / PassRole), so
instead of one Lambda per handler we expose the section 7 API contract as
routes on a single server, running in the same account as the live
DynamoDB / Bedrock / SES / EventBridge.

    POST /chat                 -> nurse_agent.handle_chat        (brief / Q&A / logging)
    GET  /dashboard            -> tables -> section 7 dashboard shape
    POST /control/outage       -> put_events(OutageDetected)  + in-process agent fan-out
    POST /control/restore      -> put_events(SystemRestored)  + SystemState flip
    POST /deliveries/{id}/done -> mark a delivery delivered

What Lambda+EventBridge did, this server does in-process:
  * /control/outage publishes OutageDetected to the `wardline` bus (kept for
    the live-feed / decoupling story) AND directly runs the alert + routing
    agents, because there is no Lambda target subscribed to the bus.
  * A background scheduler thread re-runs the alert ack-sweep and the routing
    scan on a fixed interval, replacing EventBridge Scheduler.

The agent/tool code is unchanged: this module only wires existing entry
points (nurse_agent.handle_chat, routing_agent.handle_routing_event,
alert_agent.run_ack_sweep) to HTTP routes and a timer.

Run:
    DATA_BACKEND=dynamodb AWS_REGION=us-east-1 \
      uvicorn backend.server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.agents.nurse_agent import app as nurse_agent
from backend.shared import db
from backend.shared.events import put_event

AGENT_SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "300"))  # 5-min routing scan + ack check
CONTROL_SOURCE = "control"  # the only source allowed to publish OutageDetected / SystemRestored (events.py)

app = FastAPI(title="Wardline", version="1.0")

# Section 7 says CORS enabled; the S3-hosted frontend calls this from another origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "backend": "dynamodb" if db.use_dynamodb() else "json"}


# ---------------------------------------------------------------------------
# POST /chat  -> nurse agent (host-agnostic handle_chat)
# ---------------------------------------------------------------------------

@app.post("/chat")
async def chat(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Request body must be valid JSON."})
    try:
        payload = nurse_agent.handle_chat(body)
        return JSONResponse(status_code=200, content=json.loads(json.dumps(payload, default=str)))
    except nurse_agent.ChatError as exc:
        return JSONResponse(status_code=exc.status, content={"error": exc.message})


# ---------------------------------------------------------------------------
# GET /dashboard  -> section 7 shape
#   {system, staff:[{name, acked}], escalations:[..], deliveries:[..],
#    feed:[{ts, agent, action, detail}]}
# ---------------------------------------------------------------------------

@app.get("/dashboard")
def dashboard() -> JSONResponse:
    return JSONResponse(status_code=200, content=json.loads(json.dumps(_build_dashboard(), default=str)))


def _build_dashboard() -> dict[str, Any]:
    system = _system_state()
    return {
        "system": system,
        "staff": _staff_ack_list(),
        "escalations": _unresolved_escalations(),
        "deliveries": _all_deliveries(),
        "feed": _recent_feed(),
    }


def _system_state() -> dict[str, Any]:
    """SystemState table (PK id='hospital'). Defaults to 'up' before any outage is simulated."""
    if db.use_dynamodb():
        item = db.get_item("system_state", id="hospital") or {}
    else:
        item = {}
    return {
        "status": item.get("status", "up"),
        "snapshot_ts": item.get("snapshot_ts"),
        "outage_started_at": item.get("outage_started_at"),
    }


def _staff_ack_list() -> list[dict[str, Any]]:
    """On-shift nurses/charge and whether each has acked the current outage alert."""
    from backend.agents.alert_agent import app as alert_app

    staff = alert_app.get_on_shift_staff()
    store = alert_app.get_ack_store()
    out = []
    for nurse in staff:
        if nurse.get("role") == "runner":
            continue
        out.append({"name": nurse.get("name"), "acked": alert_app.check_ack(nurse["nurse_id"], store=store)})
    return out


def _unresolved_escalations() -> list[dict[str, Any]]:
    from backend.tools import escalation

    return [e for e in escalation.list_escalations() if not e.get("resolved")]


def _all_deliveries() -> list[dict[str, Any]]:
    from backend.tools import deliveries as delivery_tools

    return delivery_tools.list_deliveries()


def _recent_feed(limit: int = 50) -> list[dict[str, Any]]:
    """AgentFeed rows for today, newest first -> {ts, agent, action, detail}."""
    if not db.use_dynamodb():
        return []
    from datetime import UTC, datetime

    day = datetime.now(UTC).strftime("%Y-%m-%d")
    try:
        rows = db.query("agent_feed", "day", day, newest_first=True, limit=limit)
    except Exception:  # noqa: BLE001 - dashboard must not 500 if the feed table is empty/absent
        return []
    return [
        {"ts": r.get("ts"), "agent": r.get("agent"), "action": r.get("action"), "detail": r.get("detail")}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# POST /control/outage, /control/restore
# ---------------------------------------------------------------------------

@app.post("/control/outage")
def control_outage() -> JSONResponse:
    from datetime import UTC, datetime

    started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    _set_system_state(status="down", outage_started_at=started_at)

    # Publish for the feed/story (no Lambda target is subscribed to the bus).
    _safe_put_event("OutageDetected", CONTROL_SOURCE, {"started_at": started_at})
    # Do the work in-process: alert nurses + run an initial routing scan.
    result = _fan_out_outage(started_at)
    return JSONResponse(status_code=200, content=json.loads(json.dumps(result, default=str)))


@app.post("/control/restore")
def control_restore() -> JSONResponse:
    from datetime import UTC, datetime

    restored_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    _set_system_state(status="up", outage_started_at=None)
    _safe_put_event("SystemRestored", CONTROL_SOURCE, {"restored_at": restored_at})
    return JSONResponse(status_code=200, content={"status": "up", "restored_at": restored_at})


def _fan_out_outage(started_at: str) -> dict[str, Any]:
    """In-process replacement for the EventBridge OutageDetected targets."""
    outcomes: dict[str, Any] = {"status": "down", "outage_started_at": started_at}
    try:
        from backend.agents.alert_agent import app as alert_app

        outcomes["alerts"] = alert_app.run_ack_sweep(store=alert_app.get_ack_store())
    except Exception as exc:  # noqa: BLE001
        outcomes["alerts_error"] = str(exc)
    try:
        from backend.agents.routing_agent import app as routing_app

        scan = routing_app.handle_routing_event(
            {"detail-type": "OutageDetected", "source": "control", "detail": {"started_at": started_at}}
        )
        outcomes["routing"] = {"event_type": scan.get("event_type"), "coverage": scan.get("coverage")}
    except Exception as exc:  # noqa: BLE001
        outcomes["routing_error"] = str(exc)
    return outcomes


@app.post("/escalations/{escalation_id}/resolve")
def escalation_resolve(escalation_id: str) -> JSONResponse:
    """Mark a human escalation handled so it leaves the open checklist."""
    from backend.tools import escalation

    try:
        updated = escalation.resolve_escalation(escalation_id)
    except escalation.EscalationError as exc:
        return JSONResponse(status_code=404, content={"error": str(exc)})
    return JSONResponse(status_code=200, content=json.loads(json.dumps(updated, default=str)))


# ---------------------------------------------------------------------------
# POST /deliveries/{request_id}/done
# ---------------------------------------------------------------------------

@app.post("/deliveries/{request_id}/done")
def delivery_done(request_id: str) -> JSONResponse:
    from backend.tools import deliveries as delivery_tools

    try:
        updated = delivery_tools.mark_delivered(request_id)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=404, content={"error": f"Delivery '{request_id}' not found: {exc}"})
    return JSONResponse(status_code=200, content=json.loads(json.dumps(updated, default=str)))


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _set_system_state(*, status: str, outage_started_at: str | None) -> None:
    if not db.use_dynamodb():
        return
    from datetime import UTC, datetime

    db.put_item("system_state", {
        "id": "hospital",
        "status": status,
        "snapshot_ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "outage_started_at": outage_started_at,
    })


def _safe_put_event(detail_type: str, source: str, detail: dict[str, Any]) -> None:
    """Publish to the bus but never let a bus failure break the control action."""
    try:
        put_event(detail_type, source, detail)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"event": "put_event_failed", "detail_type": detail_type, "error": str(exc)}))


# ---------------------------------------------------------------------------
# Background scheduler (replaces EventBridge Scheduler: 5-min scan + ack check)
# ---------------------------------------------------------------------------

def _scheduler_loop(stop: threading.Event) -> None:
    while not stop.wait(AGENT_SCAN_INTERVAL_SECONDS):
        # Only sweep while an outage is active.
        if _system_state().get("status") != "down":
            continue
        try:
            from backend.agents.alert_agent import app as alert_app

            alert_app.run_ack_sweep(store=alert_app.get_ack_store())
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"event": "scheduler_alert_failed", "error": str(exc)}))
        try:
            from backend.agents.routing_agent import app as routing_app

            routing_app.handle_routing_event({"detail-type": "RoutingScan", "detail": {}})
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"event": "scheduler_routing_failed", "error": str(exc)}))


_stop_event = threading.Event()


@app.on_event("startup")
def _start_scheduler() -> None:
    if os.getenv("DISABLE_SCHEDULER") == "1":
        return
    threading.Thread(target=_scheduler_loop, args=(_stop_event,), daemon=True).start()


@app.on_event("shutdown")
def _stop_scheduler() -> None:
    _stop_event.set()
