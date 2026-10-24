"""Unit tests for the in-memory delivery tool."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from backend.tools import deliveries
from backend.tools.deliveries import (
    DeliveryValidationError,
    create_delivery,
)


@pytest.fixture(autouse=True)
def clear_delivery_store() -> None:
    deliveries.reset_deliveries()
    yield
    deliveries.reset_deliveries()


def delivery_args(**overrides: Any) -> dict[str, Any]:
    values = {
        "room": "304",
        "med": "insulin",
        "due_at": "2026-10-24T02:00:00Z",
        "from_location": "Unit 6 cabinet",
        "priority": "critical",
        "runner_id": "marcus",
    }
    values.update(overrides)
    return values


def test_create_delivery_returns_assigned_delivery() -> None:
    delivery = create_delivery(**delivery_args())

    assert delivery["request_id"] == "304-insulin-2026-10-24T02:00:00Z"
    assert delivery["med"] == "insulin"
    assert delivery["from_location"] == "Unit 6 cabinet"
    assert delivery["to_room"] == "304"
    assert delivery["due_at"] == "2026-10-24T02:00:00Z"
    assert delivery["priority"] == "critical"
    assert delivery["runner_id"] == "marcus"
    assert delivery["status"] == "assigned"
    assert delivery["created_at"].endswith("Z")


def test_duplicate_request_returns_same_delivery() -> None:
    first = create_delivery(**delivery_args())
    duplicate = create_delivery(
        **delivery_args(
            med=" INSULIN ",
            from_location="Central pharmacy",
            runner_id="priya",
        )
    )

    assert duplicate == first
    assert duplicate["runner_id"] == "marcus"
    assert duplicate["from_location"] == "Unit 6 cabinet"
    assert len(deliveries._DELIVERY_STORE) == 1


def test_equivalent_utc_timestamps_share_request_id() -> None:
    first = create_delivery(**delivery_args())
    equivalent = create_delivery(
        **delivery_args(due_at="2026-10-23T19:00:00-07:00")
    )

    assert equivalent == first
    assert len(deliveries._DELIVERY_STORE) == 1


def test_distinct_microsecond_due_times_do_not_collapse() -> None:
    first = create_delivery(**delivery_args(due_at="2026-10-24T02:00:00.123456Z"))
    second = create_delivery(**delivery_args(due_at="2026-10-24T02:00:00.123457Z"))

    assert first["request_id"] != second["request_id"]
    assert len(deliveries._DELIVERY_STORE) == 2


def test_different_medication_or_due_time_creates_new_delivery() -> None:
    first = create_delivery(**delivery_args())
    second = create_delivery(**delivery_args(med="heparin"))
    third = create_delivery(**delivery_args(due_at="2026-10-24T03:00:00Z"))

    assert len({first["request_id"], second["request_id"], third["request_id"]}) == 3
    assert len(deliveries._DELIVERY_STORE) == 3


def test_build_request_id_is_the_shared_canonical_key() -> None:
    assert deliveries.build_request_id(
        " 304 ", "Insulin", "2026-10-23T19:00:00-07:00"
    ) == "304-insulin-2026-10-24T02:00:00Z"
    delivery = create_delivery(**delivery_args(med="Insulin"))
    assert delivery["request_id"] == deliveries.build_request_id(
        "304", "INSULIN", "2026-10-24T02:00:00Z"
    )
    assert deliveries.get_delivery(delivery["request_id"]) == delivery
    assert deliveries.list_deliveries() == [delivery]


def test_returned_delivery_does_not_mutate_store() -> None:
    first = create_delivery(**delivery_args())
    first["status"] = "delivered"

    second = create_delivery(**delivery_args())

    assert second["status"] == "assigned"


def test_concurrent_duplicates_create_only_one_delivery() -> None:
    def create_one(_: int) -> dict[str, Any]:
        return create_delivery(**delivery_args())

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(create_one, range(32)))

    assert len(deliveries._DELIVERY_STORE) == 1
    assert all(result["request_id"] == results[0]["request_id"] for result in results)
    assert all(result["created_at"] == results[0]["created_at"] for result in results)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("room", " "),
        ("med", ""),
        ("from_location", " "),
        ("priority", ""),
        ("runner_id", " "),
    ],
)
def test_empty_required_field_is_rejected(field: str, value: str) -> None:
    with pytest.raises(DeliveryValidationError, match=field):
        create_delivery(**delivery_args(**{field: value}))


@pytest.mark.parametrize(
    ("due_at", "message"),
    [
        ("not-a-time", "valid ISO 8601"),
        ("2026-10-24T02:00:00", "include a timezone"),
    ],
)
def test_invalid_due_at_is_rejected(due_at: str, message: str) -> None:
    with pytest.raises(DeliveryValidationError, match=message):
        create_delivery(**delivery_args(due_at=due_at))
