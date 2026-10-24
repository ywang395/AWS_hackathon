"""
Unit tests for `agents/alert_agent/app.py`.

Runs the full ack-tracking / resend / escalate cycle with
`NOTIFY_DRY_RUN=1` so no real AWS access is required, mirroring the demo
case in CLAUDE.md section 13: "Kim doesn't ack -> resend -> escalation to
Dana".
"""

import pytest

from backend.agents.alert_agent import app
from backend.shared.auth import InMemoryTokenStore

FIXTURE_NURSES = [
    {"nurse_id": "N001", "name": "Dana Whitfield", "email": "dana.whitfield@example.com", "role": "charge", "on_shift": True},
    {"nurse_id": "N002", "name": "Kim Alvarez", "email": "kim.alvarez@example.com", "role": "nurse", "on_shift": True},
    {"nurse_id": "N005", "name": "Marcus Webb", "email": "marcus.webb@example.com", "role": "runner", "on_shift": True},
]


@pytest.fixture(autouse=True)
def dry_run_env(monkeypatch):
    monkeypatch.setenv("NOTIFY_DRY_RUN", "1")


@pytest.fixture
def token_store():
    return InMemoryTokenStore()


# ---------------------------------------------------------------------------
# Staff lookup
# ---------------------------------------------------------------------------

def test_get_on_shift_staff_excludes_runners_by_default():
    staff = app.get_on_shift_staff(nurses=FIXTURE_NURSES)
    ids = {n["nurse_id"] for n in staff}
    assert ids == {"N001", "N002"}  # charge + nurse, not the runner


# ---------------------------------------------------------------------------
# Message builder
# ---------------------------------------------------------------------------

def test_build_outage_message_embeds_a_secure_link(token_store):
    nurse = FIXTURE_NURSES[1]
    message = app.build_outage_message(nurse, base_url="https://wardline.example/nurse", token_store=token_store)

    assert "Kim Alvarez" in message
    assert "https://wardline.example/nurse?token=" in message


def test_build_resend_message_mentions_the_attempt_number(token_store):
    nurse = FIXTURE_NURSES[1]
    message = app.build_resend_message(nurse, 2, base_url="https://wardline.example/nurse", token_store=token_store)

    assert "reminder (2)" in message


def test_each_build_call_mints_a_fresh_token(token_store):
    nurse = FIXTURE_NURSES[1]
    first = app.build_secure_link(nurse["nurse_id"], token_store=token_store)
    second = app.build_secure_link(nurse["nurse_id"], token_store=token_store)
    assert first != second  # a resend must not reuse an old, possibly-leaked token


# ---------------------------------------------------------------------------
# Ack tracking + send/resend
# ---------------------------------------------------------------------------

def test_send_alert_records_the_attempt_in_dry_run(token_store):
    store = app.InMemoryAckStore()
    result = app.send_alert(FIXTURE_NURSES[1], store=store, token_store=token_store)

    assert result.status == "dry_run"
    assert app.check_ack("N002", store=store) is False
    assert store.latest("N002").attempts == 1


def test_record_ack_marks_check_ack_true():
    store = app.InMemoryAckStore()
    store.record_sent("N002", "2024-01-15T02:05:00Z")
    app.record_ack("N002", store=store)

    assert app.check_ack("N002", store=store) is True


# ---------------------------------------------------------------------------
# escalate()
# ---------------------------------------------------------------------------

def test_escalate_appends_to_the_provided_list_when_no_tools_module():
    escalations = []
    record = app.escalate("no ack", nurse_id="N002", to_role="charge", escalations=escalations)

    assert record["reason"] == "no ack"
    assert record["to_role"] == "charge"
    assert record["resolved"] is False


# ---------------------------------------------------------------------------
# run_ack_sweep() -- the "Kim doesn't ack" demo case end to end
# ---------------------------------------------------------------------------

def test_run_ack_sweep_sends_first_alert_to_every_on_shift_nurse(token_store):
    store = app.InMemoryAckStore()
    outcomes = app.run_ack_sweep(store=store, nurses=FIXTURE_NURSES, token_store=token_store)

    sent = {o["nurse_id"]: o for o in outcomes}
    assert sent["N001"]["action"] == "sent"
    assert sent["N002"]["action"] == "sent"
    assert "N005" not in sent  # runner, excluded from the ack sweep


def test_run_ack_sweep_resends_to_unacked_nurses_on_the_second_pass(token_store):
    store = app.InMemoryAckStore()
    app.run_ack_sweep(store=store, nurses=FIXTURE_NURSES, token_store=token_store)

    # Dana acks, Kim does not.
    app.record_ack("N001", store=store)

    outcomes = app.run_ack_sweep(store=store, nurses=FIXTURE_NURSES, token_store=token_store)
    by_id = {o["nurse_id"]: o for o in outcomes}

    assert by_id["N001"]["action"] == "none"
    assert by_id["N001"]["acked"] is True
    assert by_id["N002"]["action"] == "resent"
    assert by_id["N002"]["attempt"] == 2


def test_run_ack_sweep_escalates_after_max_attempts_with_no_ack(token_store):
    """
    The CLAUDE.md section 13 demo case: "Kim doesn't ack -> resend ->
    escalation to Dana." Sweep repeatedly with Kim never acking; once her
    attempt count exceeds `max_attempts`, the sweep must escalate to the
    charge role instead of sending again.
    """
    store = app.InMemoryAckStore()
    escalations: list = []
    kim_only = [n for n in FIXTURE_NURSES if n["nurse_id"] == "N002"]

    last_outcomes = []
    for _ in range(app.DEFAULT_MAX_ATTEMPTS + 1):
        last_outcomes = app.run_ack_sweep(
            store=store, nurses=kim_only, token_store=token_store, escalations=escalations
        )

    assert last_outcomes[0]["action"] == "escalated"
    escalation = last_outcomes[0]["escalation"]
    assert escalation["nurse_id"] == "N002"
    assert escalation["to_role"] == "charge"


def test_new_alert_cycle_emails_everyone_again(token_store):
    """A second outage must re-alert nurses who acked or were escalated last time."""
    store = app.InMemoryAckStore()
    app.run_ack_sweep(store=store, nurses=FIXTURE_NURSES, token_store=token_store)
    app.record_ack("N001", store=store)  # Dana acked the first outage
    for _ in range(app.DEFAULT_MAX_ATTEMPTS):  # Kim never acked and gets escalated
        app.run_ack_sweep(store=store, nurses=FIXTURE_NURSES, token_store=token_store)

    assert app.start_new_alert_cycle(store=store) == 2

    outcomes = {o["nurse_id"]: o for o in app.run_ack_sweep(store=store, nurses=FIXTURE_NURSES, token_store=token_store)}
    assert outcomes["N001"]["action"] == "sent" and outcomes["N001"]["attempt"] == 1
    assert outcomes["N002"]["action"] == "sent" and outcomes["N002"]["attempt"] == 1


# ---------------------------------------------------------------------------
# Lambda handler + tool bindings
# ---------------------------------------------------------------------------

def test_handler_dispatches_outage_detected(monkeypatch):
    # Reset the module-level singleton store so this test doesn't leak
    # state from other tests sharing the same process.
    monkeypatch.setattr(app, "_handler_store_singleton", None)
    monkeypatch.setattr(app, "_load_nurses", lambda path=None: FIXTURE_NURSES)

    result = app.handler({"detail-type": "OutageDetected", "detail": {}})

    assert "outcomes" in result
    assert len(result["outcomes"]) == 2  # charge + nurse, runner excluded


def test_handler_dispatches_escalation_needed(monkeypatch):
    monkeypatch.setattr(app, "_handler_store_singleton", None)

    result = app.handler(
        {
            "detail-type": "EscalationNeeded",
            "detail": {"reason": "no stock anywhere", "room": "602", "to_role": "charge"},
        }
    )

    assert result["escalation"]["reason"] == "no stock anywhere"
    assert result["escalation"]["room"] == "602"


def test_handler_reports_unhandled_detail_type():
    result = app.handler({"detail-type": "SomethingElse", "detail": {}})
    assert "error" in result


def test_get_tool_impls_exposes_the_claude_md_tool_names():
    tools = app.get_tool_impls()
    assert set(tools.keys()) == {
        "get_on_shift_staff",
        "get_nurse_workload",
        "send_email",
        "check_ack",
        "schedule_ack_check",
        "escalate",
    }