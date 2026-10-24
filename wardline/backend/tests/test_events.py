"""Unit tests for the shared EventBridge helper."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest

from backend.shared.events import (
    EventConfigurationError,
    EventPublishError,
    EventValidationError,
    put_event,
)


class FakeEventBridgeClient:
    def __init__(
        self,
        response: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response or {
            "FailedEntryCount": 0,
            "Entries": [{"EventId": "event-123"}],
        }
        self.error = error
        self.requests: list[dict[str, Any]] = []

    def put_events(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


def test_put_event_sends_contract_entry_to_eventbridge() -> None:
    client = FakeEventBridgeClient()

    response = put_event(
        "MedRequested",
        "nurse_agent",
        {
            "request_id": "304-insulin-2026-10-23T10:00:00Z",
            "room": "304",
            "med": "insulin",
            "requested_by": "nurse-kim",
        },
        client=client,
        dry_run=False,
    )

    assert response == client.response
    assert len(client.requests) == 1
    entry = client.requests[0]["Entries"][0]
    assert entry["EventBusName"] == "wardline"
    assert entry["Source"] == "nurse_agent"
    assert entry["DetailType"] == "MedRequested"
    assert isinstance(entry["Time"], datetime)
    assert entry["Time"].tzinfo is not None
    assert json.loads(entry["Detail"])["room"] == "304"


def test_dry_run_prints_event_without_creating_client(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DRY_RUN", "1")

    result = put_event(
        "EscalationNeeded",
        "routing_agent",
        {
            "escalation_id": "esc-1",
            "reason": "No heparin stock",
            "room": "304",
            "to_role": "charge",
        },
    )

    assert result is None
    output = json.loads(capsys.readouterr().out)
    assert output["EventBusName"] == "wardline"
    assert output["DetailType"] == "EscalationNeeded"
    assert output["Source"] == "routing_agent"
    assert output["Time"].endswith("Z")
    assert json.loads(output["Detail"])["reason"] == "No heparin stock"


def test_invalid_source_for_event_contract_is_rejected() -> None:
    client = FakeEventBridgeClient()

    with pytest.raises(EventValidationError, match="cannot publish"):
        put_event(
            "MedRequested",
            "routing_agent",
            {"request_id": "request-1"},
            client=client,
            dry_run=False,
        )

    assert client.requests == []


def test_non_json_detail_is_rejected_before_client_call() -> None:
    client = FakeEventBridgeClient()

    with pytest.raises(EventValidationError, match="JSON serializable"):
        put_event(
            "MedRequested",
            "nurse_agent",
            {"requested_by": object()},
            client=client,
            dry_run=False,
        )

    assert client.requests == []


def test_partial_eventbridge_failure_is_an_error() -> None:
    client = FakeEventBridgeClient(
        response={
            "FailedEntryCount": 1,
            "Entries": [
                {
                    "ErrorCode": "ThrottlingException",
                    "ErrorMessage": "Rate exceeded",
                }
            ],
        }
    )

    with pytest.raises(EventPublishError, match="rejected 1 event") as exc_info:
        put_event(
            "SystemRestored",
            "control",
            {"restored_at": "2026-10-23T10:00:00Z"},
            client=client,
            dry_run=False,
        )

    assert exc_info.value.response == client.response


def test_client_failure_is_wrapped_as_publish_error() -> None:
    client = FakeEventBridgeClient(error=RuntimeError("network unavailable"))

    with pytest.raises(EventPublishError, match="client failed"):
        put_event(
            "AckCheck",
            "scheduler",
            {"nurse_id": "nurse-kim"},
            client=client,
            dry_run=False,
        )


def test_invalid_dry_run_environment_value_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DRY_RUN", "sometimes")

    with pytest.raises(EventConfigurationError, match="DRY_RUN must be one of"):
        put_event(
            "OutageDetected",
            "control",
            {"started_at": "2026-10-23T10:00:00Z"},
        )
