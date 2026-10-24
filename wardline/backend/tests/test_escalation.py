"""Unit tests for in-memory escalation creation and feed logging."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from backend.shared.agent_loop import AgentFeedEvent
from backend.tools import escalation
from backend.tools.escalation import (
    EscalationValidationError,
    escalate,
)


class RecordingFeed:
    def __init__(self, error: Exception | None = None) -> None:
        self.events: list[AgentFeedEvent] = []
        self.error = error

    def log(self, event: AgentFeedEvent) -> None:
        if self.error is not None:
            raise self.error
        self.events.append(event)


@pytest.fixture(autouse=True)
def clear_escalation_store() -> None:
    escalation.reset_escalations()
    yield
    escalation.reset_escalations()


def test_escalate_stores_record_and_writes_feed_event() -> None:
    feed = RecordingFeed()

    record = escalate(
        "routing_agent",
        "No heparin stock is available.",
        "304",
        "charge",
        feed_logger=feed,
    )

    assert record["escalation_id"]
    assert record["source_agent"] == "routing_agent"
    assert record["reason"] == "No heparin stock is available."
    assert record["room"] == "304"
    assert record["to_role"] == "charge"
    assert record["created_at"].endswith("Z")
    assert record["resolved"] is False

    assert escalation._ESCALATION_STORE[record["escalation_id"]] == record
    assert len(feed.events) == 1
    event = feed.events[0]
    assert event.agent == "routing_agent"
    assert event.action == "escalation_created"
    assert event.detail == record


def test_returned_escalation_does_not_mutate_store() -> None:
    record = escalate(
        "routing_agent",
        "Review required.",
        "304",
        "charge",
        feed_logger=RecordingFeed(),
    )
    record["resolved"] = True

    stored = escalation._ESCALATION_STORE[record["escalation_id"]]
    assert stored["resolved"] is False


def test_feed_failure_does_not_store_escalation() -> None:
    feed = RecordingFeed(error=RuntimeError("feed unavailable"))

    with pytest.raises(RuntimeError, match="feed unavailable"):
        escalate(
            "routing_agent",
            "Review required.",
            "304",
            "charge",
            feed_logger=feed,
        )

    assert escalation._ESCALATION_STORE == {}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_agent", ""),
        ("reason", " "),
        ("to_role", " "),
    ],
)
def test_empty_required_field_is_rejected(field: str, value: str) -> None:
    values: dict[str, Any] = {
        "source_agent": "routing_agent",
        "reason": "Review required.",
        "room": "304",
        "to_role": "charge",
    }
    values[field] = value

    with pytest.raises(EscalationValidationError, match=field):
        escalate(**values, feed_logger=RecordingFeed())


def test_concurrent_escalations_are_all_stored_and_logged() -> None:
    feed = RecordingFeed()

    def create_one(index: int) -> dict[str, Any]:
        return escalate(
            "routing_agent",
            f"Review reason {index}",
            f"Room {index}",
            "charge",
            feed_logger=feed,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        records = list(executor.map(create_one, range(24)))

    assert len(records) == 24
    assert len({record["escalation_id"] for record in records}) == 24
    assert len(escalation._ESCALATION_STORE) == 24
    assert len(feed.events) == 24
