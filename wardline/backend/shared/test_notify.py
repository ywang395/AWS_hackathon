"""Unit tests for shared/notify.py (SES v2 email, NOTIFY_DRY_RUN, NOTIFY_TO_OVERRIDE).

The live path uses an injected fake sesv2 client, so no AWS access or boto3
is needed. The request shape asserted here is what SES v2 send_email expects.
"""

import json
import sys

import pytest

from backend.shared.notify import send_bulk_email, send_email


class FakeSes:
    def __init__(self, fail: bool = False) -> None:
        self.calls = []
        self.fail = fail

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("MessageRejected: Email address is not verified.")
        return {"MessageId": "msg-123"}


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv("NOTIFY_DRY_RUN", "0")
    monkeypatch.setenv("FROM_EMAIL", "sender@example.com")
    monkeypatch.delenv("NOTIFY_TO_OVERRIDE", raising=False)


def _logged(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_dry_run_is_the_default_and_never_touches_boto3(monkeypatch, capsys):
    monkeypatch.delenv("NOTIFY_DRY_RUN", raising=False)
    monkeypatch.setitem(sys.modules, "boto3", None)  # importing boto3 would now fail

    result = send_email("kim.alvarez@example.com", "Systems down", "Open: https://x/?token=abc")

    assert result.status == "dry_run"
    log = _logged(capsys)
    assert log["event"] == "email"
    assert "token=abc" in log["body"]  # dry runs show the link so a developer can open it


def test_live_send_uses_the_ses_v2_request_shape(live, capsys):
    ses = FakeSes()
    result = send_email("kim.alvarez@example.com", "Systems down", "Open the link", client=ses)

    assert result.status == "sent"
    assert result.message_id == "msg-123"
    call = ses.calls[0]
    assert call["FromEmailAddress"] == "sender@example.com"
    assert call["Destination"] == {"ToAddresses": ["kim.alvarez@example.com"]}
    assert call["Content"]["Simple"]["Subject"]["Data"] == "Systems down"
    assert call["Content"]["Simple"]["Body"]["Text"]["Data"] == "Open the link"
    assert "body" not in _logged(capsys)  # live logs never carry the secure-link token


def test_override_redirects_live_mail_to_one_verified_inbox(live, monkeypatch):
    monkeypatch.setenv("NOTIFY_TO_OVERRIDE", "verified@example.org")
    ses = FakeSes()

    result = send_email("kim.alvarez@example.com", "Systems down", "body", client=ses)

    assert ses.calls[0]["Destination"] == {"ToAddresses": ["verified@example.org"]}
    assert result.to == "kim.alvarez@example.com"
    assert result.delivered_to == "verified@example.org"


def test_missing_sender_is_an_error_without_calling_ses(live, monkeypatch):
    monkeypatch.delenv("FROM_EMAIL")
    ses = FakeSes()

    result = send_email("kim.alvarez@example.com", "Systems down", "body", client=ses)

    assert result.status == "error"
    assert "FROM_EMAIL" in result.error
    assert ses.calls == []


def test_ses_failure_is_reported_not_raised(live):
    result = send_email("kim.alvarez@example.com", "Systems down", "body", client=FakeSes(fail=True))

    assert result.status == "error"
    assert "not verified" in result.error


def test_bulk_send_reaches_every_recipient(monkeypatch):
    monkeypatch.setenv("NOTIFY_DRY_RUN", "1")
    results = send_bulk_email(["a@example.com", "b@example.com"], "Systems down", "body")
    assert [r.to for r in results] == ["a@example.com", "b@example.com"]
    assert {r.status for r in results} == {"dry_run"}