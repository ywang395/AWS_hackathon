"""
shared/db.py -- DynamoDB helpers for every Wardline tool that has a Phase 1
JSON/in-memory backend and a Phase 2 DynamoDB backend.

One switch picks the backend for the whole process:

    DATA_BACKEND=json      (default) data/*.json fixtures + in-memory stores
    DATA_BACKEND=dynamodb  the CLAUDE.md section 5 tables from template.yaml

Callers never build boto3 objects. They name a table by its logical key
("patients", "tokens", ...) and get plain Python values back. DynamoDB
returns numbers as Decimal and rejects float on write, so every helper
converts at the boundary (to_dynamo / from_dynamo). scripts/seed.py uses
the same conversion, so seeded vitals such as temp_f survive the trip.

Table names come from the env vars template.yaml exports (NURSES_TABLE,
PATIENTS_TABLE, ...) and fall back to the section 5 names, so a renamed or
stage-suffixed table only needs an env var change.

Region: DYNAMODB_REGION, then AWS_REGION, then us-east-1
(shared/config.resolve_region), the same rule as Bedrock, SES and EventBridge.

Also home of DynamoAgentFeed, the AgentFeed writer that
shared/agent_loop.default_feed_logger() returns when AGENT_FEED_TABLE is set.

boto3 is imported lazily: DATA_BACKEND=json and the unit tests run without
boto3 installed. Tests inject a fake resource with set_resource().
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from backend.shared.config import resolve_region

if TYPE_CHECKING:  # type hint only: agent_loop imports this module lazily, keep it one-way
    from backend.shared.agent_loop import AgentFeedEvent

# Logical key -> (env var, default table name). Mirrors CLAUDE.md section 5
# and the Globals.Function.Environment block in template.yaml.
TABLES: dict[str, tuple[str, str]] = {
    "nurses": ("NURSES_TABLE", "Nurses"),
    "patients": ("PATIENTS_TABLE", "Patients"),
    "inventory": ("INVENTORY_TABLE", "Inventory"),
    "deliveries": ("DELIVERIES_TABLE", "Deliveries"),
    "downtime_log": ("DOWNTIME_LOG_TABLE", "DowntimeLog"),
    "tokens": ("TOKENS_TABLE", "Tokens"),
    "alerts": ("ALERTS_TABLE", "Alerts"),
    "escalations": ("ESCALATIONS_TABLE", "Escalations"),
    "agent_feed": ("AGENT_FEED_TABLE", "AgentFeed"),
    "chat_history": ("CHAT_HISTORY_TABLE", "ChatHistory"),
    "system_state": ("SYSTEM_STATE_TABLE", "SystemState"),
}

# GSI on Patients.assigned_nurse_id (template.yaml PatientsTable).
PATIENTS_BY_NURSE_INDEX = "assigned_nurse_id-index"

_resource_override: Any | None = None
_resource_cache: Any | None = None
_last_timestamp: datetime | None = None
_timestamp_lock = threading.Lock()


def use_dynamodb() -> bool:
    """True when DATA_BACKEND=dynamodb. Read on every call so tests can flip it."""
    return os.environ.get("DATA_BACKEND", "json").strip().lower() == "dynamodb"


def unique_timestamp() -> str:
    """UTC ISO 8601 with microseconds, strictly increasing within this process.

    Used for DynamoDB sort keys (DowntimeLog.ts, Alerts.sent_at,
    ChatHistory.ts). Two writes in the same clock tick would share a key and
    the second would silently overwrite the first. Some clocks (Windows)
    return the same microsecond twice, so the value is bumped by 1us when needed.
    """
    global _last_timestamp
    with _timestamp_lock:
        now = datetime.now(timezone.utc)
        if _last_timestamp is not None and now <= _last_timestamp:
            now = _last_timestamp + timedelta(microseconds=1)
        _last_timestamp = now
    return now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def table_name(key: str) -> str:
    env_var, default = TABLES[key]
    return os.environ.get(env_var, default)


def set_resource(resource: Any | None) -> None:
    """Inject a boto3-compatible DynamoDB resource (tests), or None to reset."""
    global _resource_override
    _resource_override = resource


def _resource(region_name: str | None = None) -> Any:
    """The injected test resource, else a boto3 resource. The default region's resource is cached."""
    global _resource_cache
    if _resource_override is not None:
        return _resource_override
    if region_name:
        import boto3  # lazy: only needed when DynamoDB is actually used

        return boto3.resource("dynamodb", region_name=region_name)
    if _resource_cache is None:
        import boto3

        _resource_cache = boto3.resource("dynamodb", region_name=resolve_region(None, "DYNAMODB_REGION"))
    return _resource_cache


def get_table(key: str) -> Any:
    return _resource().Table(table_name(key))


def to_dynamo(value: Any) -> Any:
    """Convert floats to Decimal, recursively. DynamoDB rejects float."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return Decimal(str(value))  # str() keeps 98.2 as 98.2, not 98.2000000000000028
    if isinstance(value, Mapping):
        return {k: to_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_dynamo(v) for v in value]
    return value


def from_dynamo(value: Any) -> Any:
    """Convert Decimal back to int or float, recursively, so results are JSON-ready."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, Mapping):
        return {k: from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_dynamo(v) for v in value]
    return value


def put_item(key: str, item: Mapping[str, Any]) -> None:
    get_table(key).put_item(Item=to_dynamo(dict(item)))


def get_item(key: str, **primary_key: Any) -> dict[str, Any] | None:
    item = get_table(key).get_item(Key=to_dynamo(primary_key)).get("Item")
    return from_dynamo(item) if item is not None else None


def delete_item(key: str, **primary_key: Any) -> None:
    get_table(key).delete_item(Key=to_dynamo(primary_key))


def update_item(key: str, primary_key: Mapping[str, Any], updates: Mapping[str, Any]) -> None:
    """SET every field in `updates` on one item."""
    names = {f"#f{i}": field for i, field in enumerate(updates)}
    values = {f":v{i}": to_dynamo(v) for i, v in enumerate(updates.values())}
    expression = "SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(updates)))
    get_table(key).update_item(
        Key=to_dynamo(dict(primary_key)),
        UpdateExpression=expression,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def query(
    key: str,
    partition_attr: str,
    partition_value: Any,
    *,
    index_name: str | None = None,
    newest_first: bool = False,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """All items sharing one partition key, sort-key order (newest_first flips it).

    Uses string expressions instead of boto3.dynamodb.conditions so this
    module never imports boto3 at load time.
    """
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": "#pk = :pk",
        "ExpressionAttributeNames": {"#pk": partition_attr},
        "ExpressionAttributeValues": {":pk": to_dynamo(partition_value)},
        "ScanIndexForward": not newest_first,
    }
    if index_name:
        kwargs["IndexName"] = index_name
    if limit:
        kwargs["Limit"] = limit
    return _paginate(get_table(key).query, kwargs, limit)


def scan(key: str) -> list[dict[str, Any]]:
    """Every item in a table. Fine for the demo-sized tables in section 5."""
    return _paginate(get_table(key).scan, {}, None)


def _paginate(call: Callable[..., Any], kwargs: dict[str, Any], limit: int | None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    while True:
        page = call(**kwargs)
        items.extend(from_dynamo(item) for item in page.get("Items", []))
        last_key = page.get("LastEvaluatedKey")
        if not last_key or (limit and len(items) >= limit):
            break
        kwargs = {**kwargs, "ExclusiveStartKey": last_key}
    return items[:limit] if limit else items


# ---------------------------------------------------------------------------
# AgentFeed writer (CLAUDE.md section 5: PK day, SK ts). Chosen by
# shared/agent_loop.default_feed_logger() whenever AGENT_FEED_TABLE is set,
# independent of DATA_BACKEND, so every agent's decisions reach the dashboard.
# ---------------------------------------------------------------------------

# DynamoDB items max out at 400 KB; keep the detail well under that.
MAX_DETAIL_BYTES = 300_000
_MAX_KEY_ATTEMPTS = 5


class DynamoError(RuntimeError):
    """Raised when a DynamoDB helper cannot complete its write."""


def to_dynamo_value(value: Any) -> Any:
    """JSON round-trip for arbitrary feed detail: floats become Decimal, other types become str.

    Unlike to_dynamo(), this also copes with values that are not plain JSON
    (datetimes, tuples), which tool results passed into AgentFeed can hold.
    """
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


def _agent_feed_table_name() -> str:
    return table_name("agent_feed")  # AGENT_FEED_TABLE, default "AgentFeed"


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
        self._table_name = table_name or _agent_feed_table_name()
        self._region_name = region_name

    def _active_table(self) -> Any:
        if self._table is None:
            self._table = _resource(self._region_name).Table(self._table_name)
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
