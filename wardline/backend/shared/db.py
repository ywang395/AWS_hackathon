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

boto3 is imported lazily: DATA_BACKEND=json and the unit tests run without
boto3 installed. Tests inject a fake resource with set_resource().
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

DEFAULT_REGION = "us-east-1"  # CLAUDE.md section 3: one region for everything

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


def _resource() -> Any:
    global _resource_cache
    if _resource_override is not None:
        return _resource_override
    if _resource_cache is None:
        import boto3  # lazy: only needed when DATA_BACKEND=dynamodb

        region = (
            os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or DEFAULT_REGION
        )
        _resource_cache = boto3.resource("dynamodb", region_name=region)
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
