"""
Tests for the two go-live scripts (scripts/go_live_dynamodb.py,
scripts/go_live_email.py) and their placeholder guard (scripts/live_config.py).

Everything runs against the existing fakes -- FakeDynamoResource for
DynamoDB and an injected fake SES client -- so the go-live glue is proven
without AWS, exactly as it will run live.
"""

import importlib

import pytest

from backend.shared import notify
from scripts import go_live_dynamodb, go_live_email, live_config


@pytest.fixture(autouse=True)
def reload_config(monkeypatch):
    """live_config reads env at import, so reload it after each monkeypatch set."""
    def _reload():
        importlib.reload(live_config)
        importlib.reload(go_live_dynamodb)
        importlib.reload(go_live_email)
    return _reload


# --- live_config placeholder guard ---------------------------------------

def test_require_rejects_a_leftover_placeholder():
    with pytest.raises(SystemExit) as exc:
        live_config.require("NOTIFY_TO_OVERRIDE", "CHANGE_ME-verified-inbox@example.com")
    assert "NOTIFY_TO_OVERRIDE" in str(exc.value)


def test_require_accepts_a_real_value():
    assert live_config.require("FROM_EMAIL", "lintuwun@gmail.com") == "lintuwun@gmail.com"


def test_env_var_overrides_the_placeholder(monkeypatch, reload_config):
    monkeypatch.setenv("NOTIFY_TO_OVERRIDE", "verified@example.org")
    reload_config()
    assert live_config.NOTIFY_TO_OVERRIDE == "verified@example.org"
    assert not live_config.is_placeholder(live_config.NOTIFY_TO_OVERRIDE)


# --- go_live_dynamodb: seed + verify against the fake --------------------

def test_dynamodb_go_live_seeds_and_passes_every_check(fake_dynamo, capsys):
    assert go_live_dynamodb.main([]) == 0
    out = capsys.readouterr().out
    assert "ALL CHECKS PASSED" in out
    assert "[FAIL]" not in out
    # Seed actually populated the fake tables.
    assert len(fake_dynamo.Table("Patients").items) == 16
    # The verify step cleaned up its test token.
    assert fake_dynamo.Table("Tokens").items == {}


def test_dynamodb_go_live_dry_run_counts_without_reading(fake_dynamo, capsys):
    assert go_live_dynamodb.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "[dry-run] would write 16 items to table 'Patients'" in out
    assert "read-back" not in out
    assert fake_dynamo.Table("Patients").items == {}  # dry run writes nothing


# --- go_live_email: real-send path with a fake SES client ----------------

def test_email_go_live_requires_a_verified_recipient(monkeypatch, reload_config):
    monkeypatch.delenv("NOTIFY_TO_OVERRIDE", raising=False)  # leave the CHANGE_ME placeholder
    monkeypatch.setenv("NOTIFY_DRY_RUN", "1")  # registered so nothing leaks even if validation moves
    reload_config()
    with pytest.raises(SystemExit) as exc:
        go_live_email.main(["--test-only"])
    assert "NOTIFY_TO_OVERRIDE" in str(exc.value)


def test_email_go_live_sends_test_and_outage_sweep(monkeypatch, reload_config, capsys):
    monkeypatch.setenv("NOTIFY_TO_OVERRIDE", "verified@example.org")
    monkeypatch.setenv("FROM_EMAIL", "lintuwun@gmail.com")
    monkeypatch.setenv("NOTIFY_DRY_RUN", "1")  # registered so _configure()'s "0" is reverted after the test
    reload_config()

    sent = []

    class FakeSes:
        def send_email(self, **kwargs):
            sent.append(kwargs)
            return {"MessageId": f"msg-{len(sent)}"}

    # Patch the client factory so notify.send_email uses the fake in live mode.
    monkeypatch.setattr(notify, "_ses_client", lambda: FakeSes())

    assert go_live_email.main([]) == 0
    capsys.readouterr()  # drain our stdout so it never leaks into another test's capsys
    # 1 test email + 4 on-shift nurses (Dana, Kim, Jordan, Sam); runners excluded.
    assert len(sent) == 5
    assert {addr for call in sent for addr in call["Destination"]["ToAddresses"]} == {"verified@example.org"}
    assert all(call["FromEmailAddress"] == "lintuwun@gmail.com" for call in sent)


def test_email_go_live_test_only_sends_one(monkeypatch, reload_config, capsys):
    monkeypatch.setenv("NOTIFY_TO_OVERRIDE", "verified@example.org")
    monkeypatch.setenv("NOTIFY_DRY_RUN", "1")  # registered so _configure()'s "0" is reverted after the test
    reload_config()
    sent = []
    monkeypatch.setattr(notify, "_ses_client", lambda: type("S", (), {"send_email": lambda self, **k: sent.append(k) or {"MessageId": "m"}})())

    assert go_live_email.main(["--test-only"]) == 0
    capsys.readouterr()
    assert len(sent) == 1