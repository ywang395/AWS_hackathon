"""
Unit tests for `shared/sms.py`.

The dry-run tests need no AWS access and no boto3 package at all (the
module only imports boto3 on the non-dry-run path). The "sent" / "error"
tests inject a fake `boto3` module via `sys.modules` so the business
logic (result shape, logging, bulk fan-out) is exercised deterministically
without ever making a real network call.
"""

import json
import sys
import types

import pytest

from backend.shared.sms import send_bulk_sms, send_sms


@pytest.fixture(autouse=True)
def dry_run_env(monkeypatch):
    """Every test in this file defaults to SMS_DRY_RUN=1 unless overridden."""
    monkeypatch.setenv("SMS_DRY_RUN", "1")


def test_send_sms_dry_run_does_not_touch_boto3(monkeypatch, capsys):
    # Make sure boto3 is NOT importable, to prove the dry-run path never
    # tries to use it.
    monkeypatch.setitem(sys.modules, "boto3", None)

    result = send_sms("+15555550101", "hello nurse")

    assert result.status == "dry_run"
    assert result.to == "+15555550101"
    assert result.message_id is None

    log_line = capsys.readouterr().out.strip()
    payload = json.loads(log_line)
    assert payload["event"] == "sms"
    assert payload["status"] == "dry_run"


def test_send_bulk_sms_dry_run_sends_to_every_recipient():
    results = send_bulk_sms(["+15555550101", "+15555550102"], "systems are down")
    assert [r.to for r in results] == ["+15555550101", "+15555550102"]
    assert all(r.status == "dry_run" for r in results)


def test_send_sms_live_path_reports_sent_with_fake_boto3(monkeypatch):
    monkeypatch.setenv("SMS_DRY_RUN", "0")

    fake_sns_client = types.SimpleNamespace(publish=lambda PhoneNumber, Message: {"MessageId": "abc-123"})
    fake_boto3 = types.SimpleNamespace(client=lambda service_name: fake_sns_client)
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

    result = send_sms("+15555550101", "hello nurse")

    assert result.status == "sent"
    assert result.message_id == "abc-123"


def test_send_sms_live_path_reports_error_on_failure(monkeypatch):
    monkeypatch.setenv("SMS_DRY_RUN", "0")

    def _raise(*args, **kwargs):
        raise RuntimeError("SNS unavailable")

    fake_sns_client = types.SimpleNamespace(publish=_raise)
    fake_boto3 = types.SimpleNamespace(client=lambda service_name: fake_sns_client)
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

    result = send_sms("+15555550101", "hello nurse")

    assert result.status == "error"
    assert "SNS unavailable" in result.error