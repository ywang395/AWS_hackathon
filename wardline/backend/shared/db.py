"""DynamoDB helpers shared by Wardline agents."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from backend.shared.agent_loop import AgentFeedEvent
from backend.shared.config import resolve_region

DEFAULT_AGENT_FEED_TABLE = "AgentFeed"
# DynamoDB items max out at 400 KB; keep the detail well under that.
MAX_DETAIL_BYTES = 300_000
_MAX_KEY_ATTEMPTS = 5


class DynamoError(RuntimeError):
    """Raised when a DynamoDB helper cannot complete its write."""


def get_table(table_name: str, *, region_name: str | None = None) -> Any:
    """Return a boto3 DynamoDB Table resource."""

    import boto3

    return boto3.resource(
        "dynamodb",
        region_name=resolve_region(region_name, "DYNAMODB_REGION"),
    ).Table(table_name)


def to_dynamo_value(value: Any) -> Any:
    """Convert JSON-like data to DynamoDB-safe types (floats become Decimal)."""

    return json.loads(json.dumps(value, default=str), parse_float=Decimal)


def _is_conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    return response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _bump_ts(ts: str) -> str:
    parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    bumped = parsed + timedelta(microseconds=1)
    return bumped.isoformat(timespec="microseconds").replace("+00:00", "Z")


class DynamoAgentFeed:
    """AgentFeed logger writing ``{day, ts, agent, action, detail}`` items.

    Writes are conditional on the key being new, so two processes logging in
    the same microsecond never overwrite each other; the loser retries with
    ``ts`` bumped by 1us.
    """

    def __init__(
        self,
        table_name: str | None = None,
        *,
        table: Any | None = None,
        region_name: str | None = None,
    ) -> None:
        self._table = table
        self._table_name = (
            table_name or os.getenv("AGENT_FEED_TABLE") or DEFAULT_AGENT_FEED_TABLE
        )
        self._region_name = region_name

    def _active_table(self) -> Any:
        if self._table is None:
            self._table = get_table(self._table_name, region_name=self._region_name)
        return self._table

    def log(self, event: AgentFeedEvent) -> None:
        detail = to_dynamo_value(dict(event.detail))
        encoded = json.dumps(detail, default=str)
        if len(encoded.encode("utf-8")) > MAX_DETAIL_BYTES:
            detail = {"truncated": True, "preview": encoded[:2000]}

        current = event
        for _ in range(_MAX_KEY_ATTEMPTS):
            item = {
                "day": current.day,
                "ts": current.ts,
                "agent": current.agent,
                "action": current.action,
                "detail": detail,
            }
            try:
                self._active_table().put_item(
                    Item=item,
                    ConditionExpression="attribute_not_exists(#ts)",
                    ExpressionAttributeNames={"#ts": "ts"},
                )
                return
            except Exception as exc:
                if not _is_conditional_failure(exc):
                    raise DynamoError("AgentFeed put_item failed.") from exc
                current = replace(current, ts=_bump_ts(current.ts))
        raise DynamoError("AgentFeed key stayed in use after retries.")
