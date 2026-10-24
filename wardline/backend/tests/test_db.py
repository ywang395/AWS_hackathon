"""Unit tests for the DynamoDB AgentFeed writer and feed timestamps (no AWS)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any

import pytest

from backend.shared.agent_loop import (
    AgentFeedEvent,
    JsonLoggingAgentFeed,
    default_feed_logger,
    next_feed_timestamp,
)
from backend.shared.db import DynamoAgentFeed, DynamoError


class ConditionalCheckFailed(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeTable:
    def __init__(self, fail_first: int = 0, error: Exception | None = None) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.fail_first = fail_first
        self.error = error

    def put_item(self, **request: Any) -> None:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        item = request["Item"]
        key = (item["day"], item["ts"])
        if self.fail_first > 0 or key in self.items:
            self.fail_first -= 1
            raise ConditionalCheckFailed()
        self.items[key] = item


def event(ts: str = "2024-01-15T02:20:00.000001Z", **detail: Any) -> AgentFeedEvent:
    return AgentFeedEvent(ts=ts, agent="routing_agent", action="tool_completed", detail=detail)


def test_feed_timestamps_are_unique_under_concurrency() -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        stamps = list(pool.map(lambda _: next_feed_timestamp(), range(500)))

    assert len(set(stamps)) == 500
    assert all(stamp.endswith("Z") for stamp in stamps)


def test_event_day_is_partition_key() -> None:
    assert event().day == "2024-01-15"


def test_dynamo_feed_writes_contract_item_with_decimals() -> None:
    table = FakeTable()

    DynamoAgentFeed(table=table).log(event(qty=1.5, location="Unit 6 cabinet"))

    request = table.calls[0]
    assert request["ConditionExpression"] == "attribute_not_exists(#ts)"
    assert request["Item"] == {
        "day": "2024-01-15",
        "ts": "2024-01-15T02:20:00.000001Z",
        "agent": "routing_agent",
        "action": "tool_completed",
        "detail": {"qty": Decimal("1.5"), "location": "Unit 6 cabinet"},
    }


def test_dynamo_feed_bumps_ts_on_key_collision() -> None:
    table = FakeTable(fail_first=1)

    DynamoAgentFeed(table=table).log(event())

    assert list(table.items) == [("2024-01-15", "2024-01-15T02:20:00.000002Z")]


def test_dynamo_feed_wraps_other_errors() -> None:
    with pytest.raises(DynamoError, match="put_item failed"):
        DynamoAgentFeed(table=FakeTable(error=RuntimeError("throttled"))).log(event())


def test_default_feed_logger_uses_dynamo_only_when_table_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENT_FEED_TABLE", raising=False)
    assert isinstance(default_feed_logger(), JsonLoggingAgentFeed)

    monkeypatch.setenv("AGENT_FEED_TABLE", "AgentFeed")
    assert isinstance(default_feed_logger(), DynamoAgentFeed)
