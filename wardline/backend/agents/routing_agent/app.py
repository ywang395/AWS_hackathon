"""AWS Lambda entry point for the Wardline routing agent."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

try:
    from backend.agents.routing_agent.tool_specs import ROUTING_TOOL_SPECS
    from backend.shared.agent_loop import AgentFeedLogger, AgentLoop, JsonLoggingAgentFeed
    from backend.tools.deliveries import create_delivery
    from backend.tools.escalation import escalate
    from backend.tools.stock import find_stock
except ModuleNotFoundError:
    # Lambda packages may expose backend/ as the import root.
    from agents.routing_agent.tool_specs import ROUTING_TOOL_SPECS
    from shared.agent_loop import AgentFeedLogger, AgentLoop, JsonLoggingAgentFeed
    from tools.deliveries import create_delivery
    from tools.escalation import escalate
    from tools.stock import find_stock

PROMPT_PATH = Path(__file__).with_name("prompt.md")
AGENT_NAME = "routing_agent"
MED_REQUESTED = "MedRequested"
SCAN_EVENT_TYPES = frozenset({"RoutingScan", "ScheduledScan", "Scheduled Event"})
ROUTING_PRIORITIES = frozenset({"critical", "urgent"})

_LOCAL_RUNNERS = (
    {
        "runner_id": "marcus",
        "location": "Unit 4",
        "workload": 0,
    },
    {
        "runner_id": "priya",
        "location": "Central pharmacy",
        "workload": 1,
    },
)


def _utc_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )


_LOCAL_DOSES = (
    {
        "patient_id": "patient-304",
        "room": "304",
        "med": "insulin",
        "due_at": _utc_timestamp(datetime.now(UTC) + timedelta(minutes=30)),
        "critical": True,
    },
    {
        "patient_id": "patient-312",
        "room": "312",
        "med": "heparin",
        "due_at": _utc_timestamp(datetime.now(UTC) + timedelta(minutes=45)),
        "critical": True,
    },
)


class RoutingEventError(RuntimeError):
    """Raised when a routing event cannot be interpreted safely."""


class RoutingToolError(RuntimeError):
    """Raised when a routing tool cannot complete its work."""


def _require_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RoutingEventError(f"{field} must be a non-empty string.")
    return value.strip()


def _require_tool_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RoutingToolError(f"{field} must be a non-empty string.")
    return value.strip()


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
            _require_text(detail.get(field), field=field)
        return MED_REQUESTED, detail

    # Scheduler invocations use "Scheduled Event"; direct local scans may set scan=true.
    if (
        detail_type in SCAN_EVENT_TYPES
        or event.get("source") == "aws.scheduler"
        or event.get("scan") is True
    ):
        return "RoutingScan", detail

    raise RoutingEventError(f"Unsupported routing event type: {detail_type!r}.")


def load_system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def get_doses_due() -> list[dict[str, Any]]:
    """Return local demo doses until the DynamoDB-backed tool is connected."""

    # The fixture is fixed for one process lifetime and replaced by DynamoDB later.
    return [dict(dose) for dose in _LOCAL_DOSES]


def get_runners() -> list[dict[str, Any]]:
    """Return local runner fixtures until the nurses table is connected."""

    return [dict(runner) for runner in _LOCAL_RUNNERS]


def create_routing_delivery(
    room: str,
    med: str,
    due_at: str,
    from_location: str,
    priority: str,
    runner_id: str,
) -> dict[str, Any]:
    """Validate model-selected stock and runner before creating a delivery."""

    normalized_med = _require_tool_text(med, field="med")
    normalized_location = _require_tool_text(
        from_location,
        field="from_location",
    )
    normalized_runner = _require_tool_text(runner_id, field="runner_id")
    normalized_room = _require_tool_text(room, field="room")
    normalized_due_at = _require_tool_text(due_at, field="due_at")
    normalized_priority = _require_tool_text(priority, field="priority")
    available_locations = {
        item["location"]
        for item in find_stock(normalized_med)
        if item["qty"] > 0
    }
    if normalized_location not in available_locations:
        raise RoutingToolError(
            f"Stock location '{normalized_location}' was not returned for "
            f"'{normalized_med}'."
        )
    valid_runners = {runner["runner_id"] for runner in get_runners()}
    if normalized_runner not in valid_runners:
        raise RoutingToolError(f"Unknown runner_id: '{normalized_runner}'.")
    if normalized_priority not in ROUTING_PRIORITIES:
        raise RoutingToolError(
            f"priority must be one of: {', '.join(sorted(ROUTING_PRIORITIES))}."
        )

    due_record = next(
        (
            dose
            for dose in get_doses_due()
            if dose["room"] == normalized_room
            and dose["med"].casefold() == normalized_med.casefold()
            and dose["due_at"] == normalized_due_at
        ),
        None,
    )
    if due_record is None:
        raise RoutingToolError(
            "due_at does not match a due dose for this room and medication."
        )

    return create_delivery(
        room=normalized_room,
        med=normalized_med,
        due_at=normalized_due_at,
        from_location=normalized_location,
        priority=normalized_priority,
        runner_id=normalized_runner,
    )


def send_sms(runner_id: str, subject: str, body: str) -> dict[str, Any]:
    """Provide a safe local notification stub until shared/notify.py is wired."""

    normalized_runner = _require_tool_text(runner_id, field="runner_id")
    normalized_subject = _require_tool_text(subject, field="subject")
    normalized_body = _require_tool_text(body, field="body")
    valid_runners = {runner["runner_id"] for runner in get_runners()}
    if normalized_runner not in valid_runners:
        raise RoutingToolError(f"Unknown runner_id: '{normalized_runner}'.")
    dry_run = os.getenv("NOTIFY_DRY_RUN", "1").strip().lower()
    if dry_run not in {"1", "true", "yes", "on"}:
        raise RoutingToolError(
            "SES delivery is not wired yet; enable NOTIFY_DRY_RUN for Phase 1."
        )

    print(
        json.dumps(
            {
                "tool": "send_sms",
                "mode": "dry_run",
                "runner_id": normalized_runner,
                "subject": normalized_subject,
                "body": normalized_body,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return {
        "status": "dry_run",
        "runner_id": normalized_runner,
        "subject": normalized_subject,
    }


def build_tool_impls(feed_logger: AgentFeedLogger) -> dict[str, Any]:
    # source_agent and feed_logger are bound in code, not supplied by the model.
    routing_escalate = partial(
        escalate,
        source_agent=AGENT_NAME,
        feed_logger=feed_logger,
    )
    return {
        "get_doses_due": get_doses_due,
        "find_stock": find_stock,
        "get_runners": get_runners,
        "create_delivery": create_routing_delivery,
        "send_sms": send_sms,
        "escalate": routing_escalate,
    }


def _build_user_input(event_type: str, detail: Mapping[str, Any]) -> str:
    if event_type == MED_REQUESTED:
        return (
            "Handle this MedRequested event. Resolve the dose through "
            f"get_doses_due, route it safely, and notify the runner.\n"
            f"Event detail: {json.dumps(dict(detail), sort_keys=True)}"
        )
    return (
        "Run a routing scan now. Process every dose due within the next 60 "
        "minutes. Route available stock and escalate anything that cannot be "
        "handled safely."
    )


def handle_routing_event(
    event: Mapping[str, Any],
    *,
    model_id: str | None = None,
    bedrock_client: Any | None = None,
    feed_logger: AgentFeedLogger | None = None,
) -> dict[str, Any]:
    """Run the routing agent for one EventBridge or local event."""

    event_type, detail = _parse_event(event) # return MED_REQUESTED, detail
    active_feed = (
        feed_logger if feed_logger is not None else JsonLoggingAgentFeed()
    )
    # Dependency injection keeps the full workflow runnable with a fake client.
    agent = AgentLoop(
        model_id=model_id,
        bedrock_client=bedrock_client,
        feed_logger=active_feed,
        agent_name=AGENT_NAME,
    )
    reply = agent.run(
        load_system_prompt(),
        ROUTING_TOOL_SPECS,
        _build_user_input(event_type, detail),
        build_tool_impls(active_feed),
    )
    return {
        "agent": AGENT_NAME,
        "event_type": event_type,
        "reply": reply,
    }


def lambda_handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda handler used by EventBridge and Scheduler."""

    del context
    return handle_routing_event(event)


def _local_demo() -> None:
    try:
        from backend.agents.routing_agent.fake_bedrock import (
            CannedBedrockClient,
            insulin_demo_responses,
        )
        from backend.tools.deliveries import reset_deliveries
        from backend.tools.escalation import reset_escalations
    except ModuleNotFoundError:
        from agents.routing_agent.fake_bedrock import (
            CannedBedrockClient,
            insulin_demo_responses,
        )
        from tools.deliveries import reset_deliveries
        from tools.escalation import reset_escalations

    reset_deliveries()
    reset_escalations()
    insulin_dose = next(
        dose for dose in get_doses_due() if dose["med"] == "insulin"
    )
    result = handle_routing_event(
        {
            "detail-type": MED_REQUESTED,
            "detail": {
                "request_id": "304-insulin-local",
                "room": "304",
                "med": "insulin",
                "requested_by": "nurse-kim",
            },
        },
        model_id="local-fake-model",
        bedrock_client=CannedBedrockClient(
            insulin_demo_responses(insulin_dose["due_at"])
        ),
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    _local_demo()
