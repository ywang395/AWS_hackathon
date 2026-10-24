"""In-memory escalation creation with AgentFeed auditing."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from backend.shared.agent_loop import (
    AgentFeedEvent,
    AgentFeedLogger,
    default_feed_logger,
    next_feed_timestamp,
)

# This memory store is process-local and will be replaced by DynamoDB in Phase 2.
_ESCALATION_STORE: dict[str, dict[str, Any]] = {}
# Protect feed writes and memory inserts as one serialized operation.
_STORE_LOCK = threading.Lock()


class EscalationError(RuntimeError):
    """Base exception for escalation tool failures."""


class EscalationValidationError(EscalationError):
    """Raised when escalation input does not satisfy the tool contract."""


def _require_non_empty_string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EscalationValidationError(f"{field} must be a non-empty string.")
    return value.strip()


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def reset_escalations() -> None:
    """Clear the in-memory store for local runs and tests."""

    with _STORE_LOCK:
        _ESCALATION_STORE.clear()


def list_escalations() -> list[dict[str, Any]]:
    """Return copies of every stored escalation."""

    with _STORE_LOCK:
        return [dict(record) for record in _ESCALATION_STORE.values()]


def get_escalation(escalation_id: str) -> dict[str, Any] | None:
    """Return a copy of one stored escalation, or None if it does not exist."""

    with _STORE_LOCK:
        record = _ESCALATION_STORE.get(escalation_id)
        return dict(record) if record is not None else None


def resolve_escalation(escalation_id: str) -> dict[str, Any]:
    """Mark one escalation handled by a human.

    Idempotent: resolving an already-resolved escalation returns it unchanged.
    The dashboard excludes resolved records from "Needs a human".
    """

    normalized = _require_non_empty_string(escalation_id, field="escalation_id")
    with _STORE_LOCK:
        record = _ESCALATION_STORE.get(normalized)
        if record is None:
            raise EscalationError(f"Unknown escalation '{normalized}'.")
        if not record.get("resolved"):
            record["resolved"] = True
            record["resolved_at"] = _utc_timestamp()
        return dict(record)


def escalate(
    source_agent: str,
    reason: str,
    room: str = "",
    to_role: str = "charge",
    *,
    nurse_id: str | None = None,
    feed_logger: AgentFeedLogger | None = None,
) -> dict[str, Any]:
    """
    Create an escalation and record its decision in AgentFeed.
    Alert Agent and Routing Agent should use this tool.
    """

    normalized_source = _require_non_empty_string(
        source_agent,
        field="source_agent",
    )
    normalized_reason = _require_non_empty_string(reason, field="reason")
    normalized_room = room.strip() if isinstance(room, str) else ""
    normalized_role = _require_non_empty_string(to_role, field="to_role")
    active_feed = (
        feed_logger if feed_logger is not None else default_feed_logger()
    )

    escalation = {
        "escalation_id": str(uuid4()),  # Unique key for this escalation record.
        "source_agent": normalized_source,  # Agent requesting human review.
        "reason": normalized_reason,  # Why human intervention is required.
        "room": normalized_room,  # Patient room affected by the issue.
        "nurse_id": nurse_id,  # Nurse involved, when the escalation is nurse-scoped.
        "to_role": normalized_role,  # Staff role receiving the escalation.
        "created_at": _utc_timestamp(),  # UTC time the escalation was created.
        "resolved": False,  # New escalations always start unresolved.
    }
    feed_event = AgentFeedEvent(
        ts=next_feed_timestamp(),
        agent=normalized_source,
        action="escalation_created",
        detail=dict(escalation),
    )

    with _STORE_LOCK:
        # Feed first so a logging failure leaves no unaudited escalation in memory.
        active_feed.log(feed_event)
        _ESCALATION_STORE[escalation["escalation_id"]] = escalation
        # Return a copy so callers cannot mutate the stored audit record.
        return dict(escalation)
