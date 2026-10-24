"""Unit tests for the SES email helper. No AWS calls are made."""

from __future__ import annotations

import json
from typing import Any

import pytest

from backend.shared.notify import (
    NotifyConfigurationError,
    NotifySendError,
    NotifyValidationError,
    send_email,
)


class FakeSesClient:
    def __init__(
        self,
        response: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response if response is not None else {"MessageId": "msg-1"}
        self.error = error
        self.requests: list[dict[str, Any]] = []

    def send_email(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


def test_dry_run_is_default_and_never_calls_ses(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("NOTIFY_DRY_RUN", raising=False)
    client = FakeSesClient()

    result = send_email("runner@wardline.example", "Subject", "Body", client=client)

    assert result["status"] == "dry_run"
    assert result["message_id"] is None
    assert client.requests == []
    printed = json.loads(capsys.readouterr().out)
    assert printed["event"] == "email"
    assert printed["body"] == "Body"


def test_live_send_builds_sesv2_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFY_DRY_RUN", "0")
    monkeypatch.setenv("FROM_EMAIL", "sender@wardline.example")
    client = FakeSesClient()

    result = send_email(
        "runner@wardline.example",
        "Critical delivery",
        "Room 304 insulin.",
        client=client,
    )

    assert result["status"] == "sent"
    assert result["message_id"] == "msg-1"
    assert client.requests == [
        {
            "FromEmailAddress": "sender@wardline.example",
            "Destination": {"ToAddresses": ["runner@wardline.example"]},
            "Content": {
                "Simple": {
                    "Subject": {"Data": "Critical delivery", "Charset": "UTF-8"},
                    "Body": {
                        "Text": {"Data": "Room 304 insulin.", "Charset": "UTF-8"}
                    },
                }
            },
        }
    ]


def test_live_send_requires_from_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FROM_EMAIL", raising=False)

    with pytest.raises(NotifyValidationError, match="FROM_EMAIL"):
        send_email(
            "runner@wardline.example",
            "S",
            "B",
            client=FakeSesClient(),
            dry_run=False,
        )


def test_ses_failure_and_missing_message_id_raise() -> None:
    with pytest.raises(NotifySendError, match="failed"):
        send_email(
            "runner@wardline.example",
            "S",
            "B",
            from_email="sender@wardline.example",
            client=FakeSesClient(error=RuntimeError("MessageRejected")),
            dry_run=False,
        )

    with pytest.raises(NotifySendError, match="MessageId"):
        send_email(
            "runner@wardline.example",
            "S",
            "B",
            from_email="sender@wardline.example",
            client=FakeSesClient(response={}),
            dry_run=False,
        )


@pytest.mark.parametrize(
    "to",
    ["", "not-an-address", "a@b.example, c@d.example", "a@b.example\r\nBcc: x@y.example"],
)
def test_rejects_invalid_or_multiple_recipients(to: str) -> None:
    with pytest.raises(NotifyValidationError):
        send_email(to, "S", "B", dry_run=True)


def test_rejects_multiline_subject_and_bad_dry_run_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(NotifyValidationError, match="single line"):
        send_email("runner@wardline.example", "Line1\nBcc: x", "B", dry_run=True)

    monkeypatch.setenv("NOTIFY_DRY_RUN", "maybe")
    with pytest.raises(NotifyConfigurationError):
        send_email("runner@wardline.example", "S", "B")
