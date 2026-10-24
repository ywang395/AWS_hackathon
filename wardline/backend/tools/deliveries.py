"""In-memory delivery creation with request-level idempotency."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

# This store is process-local; Phase 2 replaces it with DynamoDB persistence.
_DELIVERY_STORE: dict[str, dict[str, Any]] = {}
# Keep the check-and-insert atomic so concurrent callers cannot duplicate a delivery.
_STORE_LOCK = threading.Lock()


class DeliveryError(RuntimeError):
    """Base exception for delivery tool failures."""


class DeliveryValidationError(DeliveryError):
    """Raised when delivery input does not satisfy the tool contract."""


def _require_non_empty_string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeliveryValidationError(f"{field} must be a non-empty string.")
    return value.strip()


def _normalize_due_at(due_at: str) -> str:
    normalized = _require_non_empty_string(due_at, field="due_at")
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DeliveryValidationError(
            "due_at must be a valid ISO 8601 timestamp."
        ) from exc
    if parsed.tzinfo is None:
        raise DeliveryValidationError("due_at must include a timezone.")

    # Canonical UTC removes formatting differences without losing precise due times.
    canonical = parsed.astimezone(UTC)
    precision = "microseconds" if canonical.microsecond else "seconds"
    return canonical.isoformat(timespec=precision).replace("+00:00", "Z")


def reset_deliveries() -> None:
    """Clear the in-memory store for local runs and tests."""

    with _STORE_LOCK:
        _DELIVERY_STORE.clear()


def create_delivery(
    room: str,
    med: str,
    due_at: str,
    from_location: str,
    priority: str,
    runner_id: str,
) -> dict[str, Any]:
    """Create a delivery once per room, medication, and due time.

    Duplicate calls return a copy of the original delivery. They do not
    overwrite its runner, source, priority, or timestamps.
    """

    normalized_room = _require_non_empty_string(room, field="room")
    normalized_med = _require_non_empty_string(med, field="med")
    normalized_due_at = _normalize_due_at(due_at)
    normalized_source = _require_non_empty_string(
        from_location,
        field="from_location",
    )
    normalized_priority = _require_non_empty_string(priority, field="priority")
    normalized_runner = _require_non_empty_string(runner_id, field="runner_id")

    # Normalize key fields so casing or outer whitespace cannot defeat dedupe.
    request_id = (
        f"{normalized_room}-{normalized_med.casefold()}-{normalized_due_at}"
    )

    with _STORE_LOCK:
        existing = _DELIVERY_STORE.get(request_id)
        if existing is not None:
            # Retries receive the original routing decision without mutation.
            return dict(existing)

        delivery = {
            "request_id": request_id,
            "med": normalized_med,
            "from_location": normalized_source,
            "to_room": normalized_room,
            "due_at": normalized_due_at,
            "priority": normalized_priority,
            "runner_id": normalized_runner,
            "status": "assigned",
            "created_at": datetime.now(UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
        }
        _DELIVERY_STORE[request_id] = delivery
        return dict(delivery)
