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


def build_request_id(room: str, med: str, due_at: str) -> str:
    """Canonical delivery key ``{room}-{med}-{due_at}`` (CLAUDE.md §2).

    Every agent must build request IDs through this function (the nurse
    agent's ``request_med`` / ``get_delivery_status`` included) so casing or
    timestamp formatting can never produce two keys for the same dose.
    The medication is case-folded and ``due_at`` is canonical UTC.
    """

    normalized_room = _require_non_empty_string(room, field="room")
    normalized_med = _require_non_empty_string(med, field="med")
    return f"{normalized_room}-{normalized_med.casefold()}-{_normalize_due_at(due_at)}"


def reset_deliveries() -> None:
    """Clear the in-memory store for local runs and tests."""

    with _STORE_LOCK:
        _DELIVERY_STORE.clear()


def get_delivery(request_id: str) -> dict[str, Any] | None:
    """Return a copy of one delivery, or None if it does not exist."""

    with _STORE_LOCK:
        delivery = _DELIVERY_STORE.get(request_id)
        return dict(delivery) if delivery is not None else None


def list_deliveries() -> list[dict[str, Any]]:
    """Return copies of every stored delivery."""

    with _STORE_LOCK:
        return [dict(delivery) for delivery in _DELIVERY_STORE.values()]


_DELIVERY_STATUSES = ("assigned", "picked_up", "delivered")


def mark_delivered(request_id: str) -> dict[str, Any]:
    """Flip a delivery to 'delivered' (POST /deliveries/{id}/done).

    Idempotent: a delivery already 'delivered' is returned unchanged. Raises
    DeliveryError if the request_id is unknown.
    """

    normalized = _require_non_empty_string(request_id, field="request_id")
    with _STORE_LOCK:
        delivery = _DELIVERY_STORE.get(normalized)
        if delivery is None:
            raise DeliveryError(f"Unknown delivery '{normalized}'.")
        delivery["status"] = "delivered"
        delivery["delivered_at"] = (
            datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        )
        return dict(delivery)


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
    request_id = build_request_id(normalized_room, normalized_med, normalized_due_at)

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
