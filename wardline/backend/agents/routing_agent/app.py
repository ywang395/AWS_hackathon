"""Wardline routing agent.

Host-agnostic entry points: the backend server calls ``handle_routing_event``
in-process for ``MedRequested`` and ``OutageDetected``, and its background
scheduler calls ``run_routing_scan`` every five minutes (CLAUDE.md §3, §6).
``lambda_handler`` is kept only as an ``(event, context)`` adapter.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from backend.agents.routing_agent.tool_specs import ROUTING_TOOL_SPECS
from backend.shared.agent_loop import (
    AgentFeedEvent,
    AgentFeedLogger,
    AgentLoop,
    AgentLoopError,
    default_feed_logger,
    next_feed_timestamp,
)
from backend.shared import db
from backend.shared.events import put_event
from backend.shared.notify import dry_run_enabled as notify_dry_run_enabled
from backend.shared.notify import send_email as notify_send_email
from backend.tools.deliveries import (
    DeliveryError,
    build_request_id,
    create_delivery,
    get_delivery,
    list_deliveries,
)
from backend.tools.escalation import escalate, get_escalation
from backend.tools.stock import find_stock

EventPublisher = Callable[..., Any]

PROMPT_PATH = Path(__file__).with_name("prompt.md")
# data/*.json for DATA_BACKEND=json; _load_records() reads DynamoDB otherwise.
DATA_DIR = Path(__file__).resolve().parents[3] / "data"
PATIENTS_PATH = DATA_DIR / "patients.json"
NURSES_PATH = DATA_DIR / "nurses.json"

AGENT_NAME = "routing_agent"
MED_REQUESTED = "MedRequested"
OUTAGE_DETECTED = "OutageDetected"
ESCALATION_NEEDED = "EscalationNeeded"
ROUTING_SCAN = "RoutingScan"
SCAN_EVENT_TYPES = frozenset({ROUTING_SCAN, "ScheduledScan", "Scheduled Event"})
ROUTING_PRIORITIES = frozenset({"critical", "urgent"})
DOSE_WINDOW_MINUTES = 60
# One request: doses, stock, runners, delivery, notify, final reply (+ slack).
MED_REQUEST_MAX_STEPS = 8
# A scan may route and escalate several doses in one run.
SCAN_MAX_STEPS = 14
PLACEHOLDER_EMAIL_DOMAIN = "wardline.example"

# (room, med) -> escalation_id, so repeat scans don't re-escalate the same dose.
_ESCALATION_INDEX: dict[tuple[str, str], str] = {}
_INDEX_LOCK = threading.Lock()


class RoutingEventError(RuntimeError):
    """Raised when a routing event cannot be interpreted safely."""


class RoutingToolError(RuntimeError):
    """Raised when a routing tool cannot complete its work."""


class RoutingDataError(RoutingToolError):
    """Raised when seed records are missing or malformed."""


def reset_routing_state() -> None:
    """Clear routing-local memory for local runs and tests."""

    with _INDEX_LOCK:
        _ESCALATION_INDEX.clear()


def _require_text(
    value: Any,
    *,
    field: str,
    error: type[RuntimeError] = RoutingToolError,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error(f"{field} must be a non-empty string.")
    return value.strip()


def _optional_tool_text(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field=field)


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso_utc(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_json_list(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RoutingDataError(f"Unable to load records from {path.name}.") from exc
    if not isinstance(data, list) or not all(isinstance(i, dict) for i in data):
        raise RoutingDataError(f"{path.name} must contain a list of objects.")
    return data


def _load_records(table_key: str, path: Path) -> list[dict[str, Any]]:
    """Patient or staff records from DynamoDB when DATA_BACKEND=dynamodb, else data/*.json.

    Same switch the nurse and alert agents use (shared/db.py). Reading JSON
    while they read DynamoDB would let a nurse's request_med carry a due_at
    the routing agent never sees, so the dose would never be routed.
    """
    if db.use_dynamodb():
        return db.scan(table_key)
    return _load_json_list(path)


def _load_staff() -> dict[str, dict[str, Any]]:
    return {
        str(record["nurse_id"]): record
        for record in _load_records("nurses", NURSES_PATH)
        if record.get("nurse_id")
    }


def all_doses() -> tuple[str, list[dict[str, Any]]]:
    """Return ``(snapshot_ts, doses)`` for every scheduled med in the records."""

    staff = _load_staff()
    patients = _load_records("patients", PATIENTS_PATH)
    snapshots = [p["snapshot_ts"] for p in patients if p.get("snapshot_ts")]
    if not snapshots:
        raise RoutingDataError("Patient records have no snapshot_ts.")
    snapshot_ts = max(snapshots, key=_parse_utc)

    doses: list[dict[str, Any]] = []
    for patient in patients:
        nurse = staff.get(str(patient.get("assigned_nurse_id")), {})
        unit = nurse.get("unit")
        for med in patient.get("meds") or []:
            if not med.get("med") or not med.get("due_at"):
                continue
            doses.append(
                {
                    "patient_id": patient.get("patient_id"),
                    "room": str(patient.get("room")),
                    "med": med["med"],
                    "due_at": med["due_at"],
                    "critical": bool(med.get("critical")),
                    "unit": unit,
                    "home_cabinet": f"{unit} cabinet" if unit else None,
                }
            )
    return snapshot_ts, doses


def _open_escalation_id(room: str, med: str) -> str | None:
    with _INDEX_LOCK:
        escalation_id = _ESCALATION_INDEX.get((room, med.casefold()))
    if escalation_id is None:
        return None
    record = get_escalation(escalation_id)
    if record is None or record.get("resolved"):
        return None
    return escalation_id


def _enrich_dose(dose: dict[str, Any]) -> dict[str, Any]:
    stocked = {item["location"] for item in find_stock(dose["med"])}
    has_home_stock = dose["home_cabinet"] in stocked
    existing = get_delivery(build_request_id(dose["room"], dose["med"], dose["due_at"]))
    return {
        **dose,
        "home_cabinet_has_stock": has_home_stock,
        "needs_routing": not has_home_stock,
        "existing_delivery": (
            {
                "request_id": existing["request_id"],
                "status": existing["status"],
                "runner_id": existing["runner_id"],
            }
            if existing is not None
            else None
        ),
        "open_escalation_id": _open_escalation_id(dose["room"], dose["med"]),
    }


def get_doses_due(room: str | None = None, med: str | None = None) -> dict[str, Any]:
    """List doses that may need routing, read from the patient records.

    With ``room``/``med`` filters (a ``MedRequested``), matching doses are
    returned regardless of due time. Without filters (a scan), only doses due
    within 60 minutes of the record snapshot are returned. The downtime clock
    is anchored to ``snapshot_ts`` because the records are frozen at that time.
    """

    room_filter = _optional_tool_text(room, field="room")
    med_filter = _optional_tool_text(med, field="med")
    snapshot_ts, doses = all_doses()
    window_end = _parse_utc(snapshot_ts) + timedelta(minutes=DOSE_WINDOW_MINUTES)

    if room_filter is not None or med_filter is not None:
        selected = [
            dose
            for dose in doses
            if (room_filter is None or dose["room"] == room_filter)
            and (med_filter is None or dose["med"].casefold() == med_filter.casefold())
        ]
    else:
        selected = [d for d in doses if _parse_utc(d["due_at"]) <= window_end]

    selected.sort(key=lambda dose: (_parse_utc(dose["due_at"]), dose["room"]))
    return {
        "snapshot_ts": snapshot_ts,
        "window_end": _iso_utc(window_end),
        "doses": [_enrich_dose(dose) for dose in selected],
    }


def _on_shift_runners() -> dict[str, dict[str, Any]]:
    return {
        nurse_id: record
        for nurse_id, record in _load_staff().items()
        if record.get("role") == "runner" and record.get("on_shift") is True
    }


def get_runners() -> list[dict[str, Any]]:
    """List on-shift runners, least busy first. Contact details stay server-side."""

    active: dict[str, int] = {}
    for delivery in list_deliveries():
        if delivery.get("status") != "delivered":
            runner = delivery.get("runner_id")
            active[runner] = active.get(runner, 0) + 1

    runners = [
        {
            "runner_id": nurse_id,
            "name": record.get("name"),
            "location": record.get("location"),
            "workload": active.get(nurse_id, 0),
        }
        for nurse_id, record in _on_shift_runners().items()
    ]
    runners.sort(key=lambda runner: (runner["workload"], runner["runner_id"]))
    return runners


def create_routing_delivery(
    room: str,
    med: str,
    due_at: str,
    from_location: str,
    priority: str,
    runner_id: str,
) -> dict[str, Any]:
    """Validate model-selected stock, runner, and dose before creating a delivery."""

    normalized_med = _require_text(med, field="med")
    normalized_location = _require_text(from_location, field="from_location")
    normalized_runner = _require_text(runner_id, field="runner_id")
    normalized_room = _require_text(room, field="room")
    normalized_due_at = _require_text(due_at, field="due_at")
    normalized_priority = _require_text(priority, field="priority")

    available_locations = {item["location"] for item in find_stock(normalized_med)}
    if normalized_location not in available_locations:
        raise RoutingToolError(
            f"Stock location '{normalized_location}' was not returned for "
            f"'{normalized_med}'."
        )
    if normalized_runner not in _on_shift_runners():
        raise RoutingToolError(f"Unknown runner_id: '{normalized_runner}'.")
    if normalized_priority not in ROUTING_PRIORITIES:
        raise RoutingToolError(
            f"priority must be one of: {', '.join(sorted(ROUTING_PRIORITIES))}."
        )

    try:
        wanted = build_request_id(normalized_room, normalized_med, normalized_due_at)
    except DeliveryError as exc:
        raise RoutingToolError(str(exc)) from exc
    _, doses = all_doses()
    record = next(
        (
            dose
            for dose in doses
            if build_request_id(dose["room"], dose["med"], dose["due_at"]) == wanted
        ),
        None,
    )
    if record is None:
        raise RoutingToolError(
            "due_at does not match a due dose for this room and medication."
        )

    # Use the record's own values so request_id matches every other agent.
    return create_delivery(
        room=record["room"],
        med=record["med"],
        due_at=record["due_at"],
        from_location=normalized_location,
        priority=normalized_priority,
        runner_id=normalized_runner,
    )


def _runner_email(runner_id: str) -> str:
    record = _on_shift_runners().get(runner_id)
    if record is None:
        raise RoutingToolError(f"Unknown runner_id: '{runner_id}'.")
    email = record.get("email")
    if isinstance(email, str) and email.strip():
        return email.strip()
    if notify_dry_run_enabled():
        # A record without an email still works in a dry run via a synthetic address.
        return f"{runner_id.lower()}@{PLACEHOLDER_EMAIL_DOMAIN}"
    raise RoutingToolError(
        f"No email address on file for '{runner_id}'; add an SES-verified "
        "email to the Nurses record."
    )


def send_email(runner_id: str, subject: str, body: str) -> dict[str, Any]:
    """Email a selected runner via SES; the address is resolved in code."""

    normalized_runner = _require_text(runner_id, field="runner_id")
    normalized_subject = _require_text(subject, field="subject")
    normalized_body = _require_text(body, field="body")
    result = notify_send_email(
        _runner_email(normalized_runner),
        normalized_subject,
        normalized_body,
    )
    if result.status == "error":
        # shared/notify never raises (the alert sweep relies on that). A routing
        # tool must fail loudly so the model escalates instead of assuming the
        # runner knows about the delivery.
        raise RoutingToolError(f"Email to runner '{normalized_runner}' failed: {result.error}")
    # Report delivery status to the model without echoing the address.
    return {
        "status": result.status,
        "runner_id": normalized_runner,
        "subject": result.subject,
        "message_id": result.message_id,
    }


def _log_feed(feed_logger: AgentFeedLogger, action: str, detail: dict[str, Any]) -> None:
    feed_logger.log(
        AgentFeedEvent(
            ts=next_feed_timestamp(),
            agent=AGENT_NAME,
            action=action,
            detail=detail,
        )
    )


def _make_routing_escalate(
    feed_logger: AgentFeedLogger,
    publish_event: EventPublisher,
) -> Callable[..., dict[str, Any]]:
    def routing_escalate(
        reason: str,
        room: str,
        to_role: str = "charge",
        med: str | None = None,
    ) -> dict[str, Any]:
        """Escalate once per (room, med) and publish EscalationNeeded (§6)."""

        normalized_room = _require_text(room, field="room")
        normalized_med = _optional_tool_text(med, field="med")
        if normalized_med is not None:
            existing_id = _open_escalation_id(normalized_room, normalized_med)
            if existing_id is not None:
                existing = get_escalation(existing_id) or {}
                return {**existing, "duplicate": True, "event_published": False}

        record = escalate(
            AGENT_NAME,
            reason,
            normalized_room,
            to_role,
            feed_logger=feed_logger,
        )
        if normalized_med is not None:
            with _INDEX_LOCK:
                _ESCALATION_INDEX[(normalized_room, normalized_med.casefold())] = (
                    record["escalation_id"]
                )

        detail = {
            "escalation_id": record["escalation_id"],
            "reason": record["reason"],
            "room": record["room"],
            "to_role": record["to_role"],
        }
        try:
            publish_event(ESCALATION_NEEDED, AGENT_NAME, detail)
        except Exception as exc:
            # The escalation is already stored and on the dashboard; report, don't retry.
            _log_feed(
                feed_logger,
                "event_publish_failed",
                {
                    "detail_type": ESCALATION_NEEDED,
                    "escalation_id": record["escalation_id"],
                    "error_type": type(exc).__name__,
                },
            )
            return {**record, "event_published": False}

        _log_feed(
            feed_logger,
            "event_published",
            {"detail_type": ESCALATION_NEEDED, **detail},
        )
        return {**record, "event_published": True}

    return routing_escalate


def build_tool_impls(
    feed_logger: AgentFeedLogger,
    publish_event: EventPublisher | None = None,
) -> dict[str, Any]:
    # source_agent, feed_logger, and the publisher are bound in code, not by the model.
    return {
        "get_doses_due": get_doses_due,
        "find_stock": find_stock,
        "get_runners": get_runners,
        "create_delivery": create_routing_delivery,
        "send_email": send_email,
        "escalate": _make_routing_escalate(
            feed_logger,
            publish_event if publish_event is not None else put_event,
        ),
    }


def _parse_event(event: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    if not isinstance(event, Mapping):
        raise RoutingEventError("event must be a mapping.")

    detail_type = (
        event.get("detail-type")
        or event.get("detail_type")
        or event.get("type")
    )
    raw_detail = event.get("detail", {})
    if isinstance(raw_detail, str):
        try:
            raw_detail = json.loads(raw_detail)
        except json.JSONDecodeError as exc:
            raise RoutingEventError("event detail must be valid JSON.") from exc
    if not isinstance(raw_detail, Mapping):
        raise RoutingEventError("event detail must be a mapping.")
    detail = dict(raw_detail)

    if detail_type == MED_REQUESTED:
        for field in ("request_id", "room", "med", "requested_by"):
            _require_text(detail.get(field), field=field, error=RoutingEventError)
        return MED_REQUESTED, detail

    if detail_type == OUTAGE_DETECTED:
        return OUTAGE_DETECTED, detail

    # Scheduler invocations use "Scheduled Event"; direct local scans may set scan=true.
    if (
        detail_type in SCAN_EVENT_TYPES
        or event.get("source") == "aws.scheduler"
        or event.get("scan") is True
    ):
        return ROUTING_SCAN, detail

    raise RoutingEventError(f"Unsupported routing event type: {detail_type!r}.")


def load_system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


_SCAN_INSTRUCTIONS = (
    "Call get_doses_due with no arguments. For every dose with needs_routing "
    "true, no existing_delivery, and no open_escalation_id, route available "
    "stock or escalate. Skip every other dose."
)


def _build_user_input(event_type: str, detail: Mapping[str, Any]) -> str:
    if event_type == MED_REQUESTED:
        return (
            "Handle this MedRequested event. Call get_doses_due with the event "
            "room and med to resolve the dose, route it safely, and notify the "
            "runner.\n"
            f"Event detail: {json.dumps(dict(detail), sort_keys=True)}"
        )
    if event_type == OUTAGE_DETECTED:
        started_at = detail.get("started_at", "unknown time")
        return (
            f"Hospital systems went down at {started_at}. Run the initial "
            f"routing scan now. {_SCAN_INSTRUCTIONS}"
        )
    return f"Run a routing scan now. {_SCAN_INSTRUCTIONS}"


def _pending_doses() -> list[dict[str, Any]]:
    """Doses in the 60-minute window that need routing but have no outcome yet."""

    return [
        dose
        for dose in get_doses_due()["doses"]
        if dose["needs_routing"]
        and dose["existing_delivery"] is None
        and dose["open_escalation_id"] is None
    ]


def _dose_label(dose: Mapping[str, Any]) -> dict[str, Any]:
    return {"room": dose["room"], "med": dose["med"], "due_at": dose["due_at"]}


def _ensure_scan_coverage(
    run_single_dose: Callable[[dict[str, Any]], str],
    routing_escalate: Callable[..., dict[str, Any]],
    feed_logger: AgentFeedLogger,
) -> dict[str, list[dict[str, Any]]]:
    """Guarantee every pending dose ends with a delivery or an escalation.

    The model is not trusted to cover every dose in a scan. Anything it left
    unhandled is retried once as a targeted single-dose request; anything still
    unhandled (including when Bedrock is unavailable) is escalated by code.
    """

    retried: list[dict[str, Any]] = []
    for dose in _pending_doses():
        label = _dose_label(dose)
        _log_feed(feed_logger, "scan_dose_unhandled", label)
        retried.append(label)
        try:
            run_single_dose(dose)
        except Exception as exc:
            _log_feed(
                feed_logger,
                "scan_retry_failed",
                {**label, "error_type": type(exc).__name__},
            )

    escalated: list[dict[str, Any]] = []
    for dose in _pending_doses():
        label = _dose_label(dose)
        routing_escalate(
            reason=(
                f"Automated routing did not complete for {dose['med']} in room "
                f"{dose['room']} (due {dose['due_at']}); manual review required."
            ),
            room=dose["room"],
            med=dose["med"],
        )
        escalated.append(label)
    return {"retried": retried, "escalated": escalated}


def handle_routing_event(
    event: Mapping[str, Any],
    *,
    model_id: str | None = None,
    bedrock_client: Any | None = None,
    feed_logger: AgentFeedLogger | None = None,
    event_publisher: EventPublisher | None = None,
    max_steps: int | None = None,
) -> dict[str, Any]:
    """Run the routing agent for one MedRequested, OutageDetected, or scan event.

    Leave ``bedrock_client`` as None to call real Bedrock using ``MODEL_ID``
    and the standard AWS credential chain (including AWS_BEARER_TOKEN_BEDROCK).
    ``event_publisher`` defaults to ``put_event`` (honors ``DRY_RUN``).

    Scan and outage runs finish with a code-enforced coverage check, reported
    under ``coverage`` in the result.
    """

    event_type, detail = _parse_event(event)
    active_feed = feed_logger if feed_logger is not None else default_feed_logger()
    tool_impls = build_tool_impls(active_feed, event_publisher)

    def run(run_type: str, run_detail: Mapping[str, Any], steps: int) -> str:
        # A fresh loop per run; dependency injection keeps tests off live AWS.
        agent = AgentLoop(
            model_id=model_id,
            bedrock_client=bedrock_client,
            feed_logger=active_feed,
            agent_name=AGENT_NAME,
        )
        return agent.run(
            load_system_prompt(),
            ROUTING_TOOL_SPECS,
            _build_user_input(run_type, run_detail),
            tool_impls,
            max_steps=steps,
        )

    if event_type == MED_REQUESTED:
        return {
            "agent": AGENT_NAME,
            "event_type": event_type,
            "reply": run(event_type, detail, max_steps or MED_REQUEST_MAX_STEPS),
        }

    reply = ""
    try:
        reply = run(event_type, detail, max_steps or SCAN_MAX_STEPS)
    except AgentLoopError as exc:
        _log_feed(active_feed, "agent_failed", {"error_type": type(exc).__name__})
        reply = f"Routing agent failed ({type(exc).__name__}); safety check applied."

    def run_single_dose(dose: dict[str, Any]) -> str:
        return run(
            MED_REQUESTED,
            {
                "request_id": build_request_id(
                    dose["room"], dose["med"], dose["due_at"]
                ),
                "room": dose["room"],
                "med": dose["med"],
                "requested_by": AGENT_NAME,
            },
            MED_REQUEST_MAX_STEPS,
        )

    coverage = _ensure_scan_coverage(
        run_single_dose, tool_impls["escalate"], active_feed
    )
    if coverage["escalated"]:
        rooms = ", ".join(f"{d['med']} room {d['room']}" for d in coverage["escalated"])
        reply = f"{reply} Safety check escalated to the charge nurse: {rooms}."
    return {
        "agent": AGENT_NAME,
        "event_type": event_type,
        "reply": reply,
        "coverage": coverage,
    }


def run_routing_scan(**kwargs: Any) -> dict[str, Any]:
    """Entry point for the server's five-minute background scheduler."""

    return handle_routing_event({"detail-type": ROUTING_SCAN, "detail": {}}, **kwargs)


def lambda_handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:
    """``(event, context)`` adapter for Lambda-style hosts."""

    del context
    return handle_routing_event(event)
