"""In-memory stand-in for a boto3 DynamoDB resource, for unit tests.

Implements what backend/shared/db.py and scripts/seed.py call: Table(name),
put_item, get_item, delete_item, update_item, query, scan, batch_writer.
put_item rejects float the way real boto3 does, so a missing to_dynamo()
conversion fails here exactly as it would against AWS.
"""

from __future__ import annotations

import copy
import re
from typing import Any

# Table -> (partition key, sort key or None, {GSI name: partition attr}).
# Mirrors CLAUDE.md section 5 and template.yaml.
KEY_SCHEMA: dict[str, tuple[str, str | None, dict[str, str]]] = {
    "Nurses": ("nurse_id", None, {}),
    "Patients": ("patient_id", None, {"assigned_nurse_id-index": "assigned_nurse_id"}),
    "Inventory": ("location", "med", {}),
    "Deliveries": ("request_id", None, {}),
    "DowntimeLog": ("patient_id", "ts", {}),
    "Tokens": ("token", None, {}),
    "Alerts": ("nurse_id", "sent_at", {}),
    "Escalations": ("escalation_id", None, {}),
    "AgentFeed": ("day", "ts", {}),
    "ChatHistory": ("token", "ts", {}),
    "SystemState": ("id", None, {}),
}


def _reject_floats(value: Any) -> None:
    if isinstance(value, float):
        raise TypeError("Float types are not supported. Use Decimal types instead.")
    if isinstance(value, dict):
        for item in value.values():
            _reject_floats(item)
    if isinstance(value, list):
        for item in value:
            _reject_floats(item)


class ConditionalCheckFailed(Exception):
    """Mimics botocore's ClientError for a failed ConditionExpression."""

    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeTable:
    def __init__(self, name: str, pk: str, sk: str | None, indexes: dict[str, str], page_size: int | None) -> None:
        self.name, self.pk, self.sk, self.indexes, self.page_size = name, pk, sk, indexes, page_size
        self.items: dict[tuple, dict] = {}

    def _key(self, item: dict) -> tuple:
        return (item[self.pk], item.get(self.sk) if self.sk else None)

    def put_item(self, Item: dict, ConditionExpression: str | None = None,
                 ExpressionAttributeNames: dict | None = None) -> dict:
        _reject_floats(Item)
        key = self._key(Item)
        # Only attribute_not_exists(...) is used (shared/db.DynamoAgentFeed).
        if ConditionExpression and ConditionExpression.startswith("attribute_not_exists") and key in self.items:
            raise ConditionalCheckFailed()
        self.items[key] = copy.deepcopy(Item)
        return {}

    def get_item(self, Key: dict) -> dict:
        item = self.items.get(self._key(Key))
        return {"Item": copy.deepcopy(item)} if item is not None else {}

    def delete_item(self, Key: dict) -> dict:
        self.items.pop(self._key(Key), None)
        return {}

    def update_item(self, Key: dict, UpdateExpression: str, ExpressionAttributeNames: dict,
                    ExpressionAttributeValues: dict) -> dict:
        _reject_floats(ExpressionAttributeValues)
        item = self.items[self._key(Key)]  # callers only update rows they just read
        for name_ref, value_ref in re.findall(r"(#\w+) = (:\w+)", UpdateExpression):
            item[ExpressionAttributeNames[name_ref]] = copy.deepcopy(ExpressionAttributeValues[value_ref])
        return {}

    def query(self, KeyConditionExpression: str, ExpressionAttributeNames: dict, ExpressionAttributeValues: dict,
              ScanIndexForward: bool = True, IndexName: str | None = None, Limit: int | None = None,
              ExclusiveStartKey: Any = None) -> dict:
        assert KeyConditionExpression == "#pk = :pk", KeyConditionExpression
        attr = ExpressionAttributeNames["#pk"]
        expected = self.indexes[IndexName] if IndexName else self.pk
        assert attr == expected, f"{self.name}: query on '{attr}' but the key is '{expected}'"
        rows = [item for item in self.items.values() if item.get(attr) == ExpressionAttributeValues[":pk"]]
        if self.sk and not IndexName:
            rows.sort(key=lambda item: item[self.sk], reverse=not ScanIndexForward)
        return {"Items": copy.deepcopy(rows[:Limit] if Limit else rows)}

    def scan(self, ExclusiveStartKey: Any = None) -> dict:
        rows = list(self.items.values())
        if self.page_size is None:
            return {"Items": copy.deepcopy(rows)}
        start = ExclusiveStartKey["offset"] if ExclusiveStartKey else 0
        response: dict[str, Any] = {"Items": copy.deepcopy(rows[start:start + self.page_size])}
        if start + self.page_size < len(rows):
            response["LastEvaluatedKey"] = {"offset": start + self.page_size}
        return response

    def batch_writer(self) -> "_BatchWriter":
        return _BatchWriter(self)


class _BatchWriter:
    def __init__(self, table: FakeTable) -> None:
        self.table = table

    def __enter__(self) -> "_BatchWriter":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def put_item(self, Item: dict) -> None:
        self.table.put_item(Item=Item)


class FakeDynamoResource:
    """Drop-in for boto3.resource("dynamodb"). page_size makes scan() paginate."""

    def __init__(self, page_size: int | None = None) -> None:
        self.tables = {name: FakeTable(name, pk, sk, idx, page_size) for name, (pk, sk, idx) in KEY_SCHEMA.items()}

    def Table(self, name: str) -> FakeTable:
        return self.tables[name]