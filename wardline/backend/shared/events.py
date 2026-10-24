"""EventBridge publishing helper for the Wardline event bus."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any


DEFAULT_EVENT_BUS_NAME = "wardline"
DEFAULT_REGION = "us-west-2"

ALLOWED_SOURCES_BY_DETAIL_TYPE = {
    "OutageDetected": frozenset({"control"}),
    "MedRequested": frozenset({"nurse_agent"}),
    "EscalationNeeded": frozenset({"routing_agent", "alert_agent"}),
    "AckCheck": frozenset({"scheduler"}),
    "SystemRestored": frozenset({"control"}),
}

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", ""})


class EventError(RuntimeError):
    """Base exception for EventBridge helper failures."""


class EventConfigurationError(EventError):
    """Raised when event configuration is missing or invalid."""


class EventValidationError(EventError):
    """Raised when an event does not satisfy the Wardline contract."""


class EventPublishError(EventError):
    """Raised when EventBridge rejects an event or the request fails."""

    def __init__(
        self,
        message: str,
        *,
        response: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.response = dict(response) if response is not None else None


def _parse_bool(value: str, *, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise EventConfigurationError(
        f"{name} must be one of: 1, 0, true, false, yes, no, on, off."
    )


def _resolve_dry_run(dry_run: bool | None) -> bool:
    if dry_run is not None:
        if not isinstance(dry_run, bool):
            raise EventConfigurationError("dry_run must be a boolean or None.")
        return dry_run
    return _parse_bool(os.getenv("DRY_RUN", "0"), name="DRY_RUN")


def _resolve_bus_name(event_bus_name: str | None) -> str:
    resolved = event_bus_name or os.getenv("EVENT_BUS_NAME") or DEFAULT_EVENT_BUS_NAME
    if not isinstance(resolved, str) or not resolved.strip():
        raise EventConfigurationError("event_bus_name must be a non-empty string.")
    return resolved.strip()


def _create_eventbridge_client(region_name: str | None) -> Any:
    resolved_region = (
        region_name
        or os.getenv("EVENTBRIDGE_REGION")
        or os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or DEFAULT_REGION
    )
    import boto3
    from botocore.config import Config

    return boto3.client(
        "events",
        region_name=resolved_region,
        config=Config(
            connect_timeout=3,
            read_timeout=10,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )


def _validate_event_contract(detail_type: str, source: str) -> None:
    allowed_sources = ALLOWED_SOURCES_BY_DETAIL_TYPE.get(detail_type)
    if allowed_sources is None:
        valid_types = ", ".join(sorted(ALLOWED_SOURCES_BY_DETAIL_TYPE))
        raise EventValidationError(
            f"Unknown DetailType '{detail_type}'. Expected one of: {valid_types}."
        )
    if source not in allowed_sources:
        valid_sources = ", ".join(sorted(allowed_sources))
        raise EventValidationError(
            f"Source '{source}' cannot publish '{detail_type}'. "
            f"Expected one of: {valid_sources}."
        )


def _serialize_detail(detail: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            dict(detail),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise EventValidationError(
            "Event detail must be JSON serializable."
        ) from exc


def _validate_entry_text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EventValidationError(f"{name} must be a non-empty string.")
    normalized = value.strip()
    if len(normalized) > 256:
        raise EventValidationError(f"{name} must be 256 characters or fewer.")
    return normalized


def put_event(
    detail_type: str,
    source: str,
    detail: Mapping[str, Any],
    *,
    event_bus_name: str | None = None,
    client: Any | None = None,
    dry_run: bool | None = None,
    region_name: str | None = None,
) -> dict[str, Any] | None:
    """Publish one Wardline event to EventBridge.

    ``DRY_RUN`` defaults to false. When true, the canonical event entry is
    printed as JSON and no AWS client is created or called.

    Returns the EventBridge response for live calls and ``None`` for dry runs.
    """

    normalized_detail_type = _validate_entry_text( # verify text 
        detail_type,
        name="detail_type",
    )
    normalized_source = _validate_entry_text(source, name="source")
    if not isinstance(detail, Mapping):
        raise EventValidationError("detail must be a mapping.")

    _validate_event_contract(normalized_detail_type, normalized_source)
    serialized_detail = _serialize_detail(detail)
    bus_name = _resolve_bus_name(event_bus_name)
    run_as_dry_run = _resolve_dry_run(dry_run)

    event_time = datetime.now(UTC)
    entry = {
        "EventBusName": bus_name,
        "Source": normalized_source,
        "DetailType": normalized_detail_type,
        "Detail": serialized_detail,
        "Time": event_time,
    }

    if run_as_dry_run:
        printable_entry = {
            **entry,
            "Time": event_time.isoformat().replace("+00:00", "Z"),
        }
        print(json.dumps(printable_entry, ensure_ascii=True, sort_keys=True))
        return None

    active_client = (
        client
        if client is not None
        else _create_eventbridge_client(region_name)
    )
    try:
        response = active_client.put_events(Entries=[entry])
    except Exception as exc:
        raise EventPublishError("EventBridge client failed.") from exc

    if not isinstance(response, Mapping):
        raise EventPublishError("EventBridge returned a non-mapping response.")

    failed_entry_count = response.get("FailedEntryCount")
    if not isinstance(failed_entry_count, int):
        raise EventPublishError(
            "EventBridge response is missing FailedEntryCount.",
            response=response,
        )
    if failed_entry_count > 0:
        raise EventPublishError(
            f"EventBridge rejected {failed_entry_count} event entry.",
            response=response,
        )

    return dict(response)
