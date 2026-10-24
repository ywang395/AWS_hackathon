"""Local integration tests for the routing agent (fake Bedrock, seed data)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from backend.shared.notify import NotifyResult

from backend.agents.routing_agent import app as routing_app
from backend.agents.routing_agent.app import (
    RoutingEventError,
    RoutingToolError,
    build_tool_impls,
    create_routing_delivery,
    get_doses_due,
    get_runners,
    handle_routing_event,
    send_email,
)
from backend.agents.routing_agent.fake_bedrock import (
    CannedBedrockClient,
    final_text,
    insulin_demo_responses,
    tool_use,
)
from backend.agents.routing_agent.cli import demo_dose
from backend.tests.helpers import RecordingFeed, RecordingPublisher
from backend.tools import deliveries, escalation
from backend.tools.deliveries import build_request_id

INSULIN_DUE_AT = "2024-01-15T02:20:00Z"  # data/patients.json, room 304


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("NOTIFY_DRY_RUN", "1")
    deliveries.reset_deliveries()
    escalation.reset_escalations()
    routing_app.reset_routing_state()
    yield
    deliveries.reset_deliveries()
    escalation.reset_escalations()
    routing_app.reset_routing_state()


def med_requested_event(med: str = "Insulin", room: str = "304") -> dict[str, Any]:
    return {
        "detail-type": "MedRequested",
        "source": "nurse_agent",
        "detail": {
            "request_id": f"{room}-{med}-local",
            "room": room,
            "med": med,
            "requested_by": "N003",
        },
    }


def test_med_requested_runs_full_fake_bedrock_workflow(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = CannedBedrockClient(insulin_demo_responses(INSULIN_DUE_AT))
    feed = RecordingFeed()

    result = handle_routing_event(
        med_requested_event(),
        model_id="fake-model",
        bedrock_client=client,
        feed_logger=feed,
    )

    assert result == {
        "agent": "routing_agent",
        "event_type": "MedRequested",
        "reply": "Insulin routed from Unit 6 to room 304 with Marcus.",
    }
    stored = deliveries.list_deliveries()
    assert len(stored) == 1
    assert stored[0]["request_id"] == "304-insulin-2024-01-15T02:20:00Z"
    assert stored[0]["from_location"] == "Unit 6 cabinet"
    assert stored[0]["runner_id"] == "N005"
    assert stored[0]["med"] == "Insulin"

    requested_tools = [
        event.detail["tool"] for event in feed.events if event.action == "tool_requested"
    ]
    assert requested_tools == [
        "get_doses_due",
        "find_stock",
        "get_runners",
        "create_delivery",
        "send_email",
    ]
    printed = json.loads(capsys.readouterr().out)
    assert printed["event"] == "email"
    assert printed["to"] == "marcus.webb@example.com"  # N005 in data/nurses.json
    assert "Unit 6 cabinet" in printed["body"]
    email_result = next(
        event.detail["result"]
        for event in feed.events
        if event.action == "tool_completed" and event.detail["tool"] == "send_email"
    )
    assert email_result["status"] == "dry_run"
    # The runner's address is resolved server-side and never returned to the model.
    assert "to" not in email_result


def test_feed_events_have_unique_increasing_keys() -> None:
    feed = RecordingFeed()
    handle_routing_event(
        med_requested_event(),
        model_id="fake-model",
        bedrock_client=CannedBedrockClient(insulin_demo_responses(INSULIN_DUE_AT)),
        feed_logger=feed,
    )

    keys = [(event.day, event.ts) for event in feed.events]
    assert len(set(keys)) == len(keys)
    assert [event.ts for event in feed.events] == sorted(e.ts for e in feed.events)


def test_scheduled_event_runs_scan_prompt() -> None:
    client = CannedBedrockClient(
        [
            tool_use("tool-1", "get_doses_due", {}),
            final_text("Scan complete; no doses remain unrouted."),
        ]
    )

    result = handle_routing_event(
        {"source": "aws.scheduler", "detail-type": "Scheduled Event", "detail": {}},
        model_id="fake-model",
        bedrock_client=client,
        feed_logger=RecordingFeed(),
    )

    assert result["event_type"] == "RoutingScan"
    user_message = client.requests[0]["messages"][0]["content"][0]["text"]
    assert "routing scan" in user_message
    assert "needs_routing" in user_message


def test_outage_detected_triggers_initial_scan() -> None:
    client = CannedBedrockClient([final_text("Records as of 02:05. Scan complete.")])

    result = handle_routing_event(
        {
            "detail-type": "OutageDetected",
            "source": "control",
            "detail": {"started_at": "2024-01-15T02:10:00Z"},
        },
        model_id="fake-model",
        bedrock_client=client,
        feed_logger=RecordingFeed(),
    )

    assert result["event_type"] == "OutageDetected"
    user_message = client.requests[0]["messages"][0]["content"][0]["text"]
    assert "2024-01-15T02:10:00Z" in user_message
    assert "initial routing scan" in user_message
    assert client.requests[0]["inferenceConfig"]


class FailingBedrockClient:
    def converse(self, **request: Any) -> dict[str, Any]:
        raise RuntimeError("bedrock unavailable")


def run_scan(client: Any, publisher: RecordingPublisher, feed: RecordingFeed) -> dict[str, Any]:
    return handle_routing_event(
        {"detail-type": "RoutingScan", "detail": {}},
        model_id="fake-model",
        bedrock_client=client,
        feed_logger=feed,
        event_publisher=publisher,
    )


def test_scan_safety_check_escalates_doses_the_model_skipped() -> None:
    publisher, feed = RecordingPublisher(), RecordingFeed()
    # The model looks at the doses, then claims it is done without routing anything.
    client = CannedBedrockClient(
        [
            tool_use("tool-1", "get_doses_due", {}),
            final_text("Scan complete."),
            # Targeted retries for insulin and heparin also give up immediately.
            final_text("Nothing to do."),
            final_text("Nothing to do."),
        ]
    )

    result = run_scan(client, publisher, feed)

    escalated = {(d["room"], d["med"]) for d in result["coverage"]["escalated"]}
    assert escalated == {("304", "Insulin"), ("602", "Heparin")}
    assert "Safety check escalated" in result["reply"]
    assert {e["room"] for e in escalation.list_escalations()} == {"304", "602"}
    assert [call[0] for call in publisher.calls] == ["EscalationNeeded"] * 2
    assert "scan_dose_unhandled" in feed.actions()
    assert not _pending_doses_left()


def test_scan_safety_check_retry_lets_the_agent_finish_the_dose() -> None:
    publisher, feed = RecordingPublisher(), RecordingFeed()
    client = CannedBedrockClient(
        [
            tool_use("tool-1", "get_doses_due", {}),
            final_text("Scan complete."),
            # Retry for insulin (sorted by due time) succeeds end to end.
            *insulin_demo_responses(INSULIN_DUE_AT),
            # Retry for heparin finds no stock and escalates properly.
            tool_use("h-1", "find_stock", {"med": "Heparin"}),
            tool_use(
                "h-2",
                "escalate",
                {
                    "reason": "No positive Heparin stock is available.",
                    "room": "602",
                    "to_role": "charge",
                    "med": "Heparin",
                },
            ),
            final_text("Heparin escalated."),
        ]
    )

    result = run_scan(client, publisher, feed)

    assert [d["to_room"] for d in deliveries.list_deliveries()] == ["304"]
    assert [e["room"] for e in escalation.list_escalations()] == ["602"]
    assert result["coverage"]["escalated"] == []
    assert len(result["coverage"]["retried"]) == 2
    assert [call[0] for call in publisher.calls] == ["EscalationNeeded"]


def test_scan_safety_check_escalates_everything_when_bedrock_is_down() -> None:
    publisher, feed = RecordingPublisher(), RecordingFeed()

    result = run_scan(FailingBedrockClient(), publisher, feed)

    assert "agent_failed" in feed.actions()
    assert {(d["room"], d["med"]) for d in result["coverage"]["escalated"]} == {
        ("304", "Insulin"),
        ("602", "Heparin"),
    }
    assert len(escalation.list_escalations()) == 2
    assert not deliveries.list_deliveries()


def test_scan_safety_check_is_quiet_when_everything_is_handled() -> None:
    publisher = RecordingPublisher()
    routing_escalate = build_tool_impls(RecordingFeed(), publisher)["escalate"]
    routing_escalate(reason="No stock.", room="602", med="Heparin")
    create_routing_delivery(
        room="304",
        med="Insulin",
        due_at=INSULIN_DUE_AT,
        from_location="Unit 6 cabinet",
        priority="critical",
        runner_id="N005",
    )
    client = CannedBedrockClient([final_text("Records as of 02:05. Nothing pending.")])

    result = run_scan(client, publisher, RecordingFeed())

    assert result["coverage"] == {"retried": [], "escalated": []}
    assert len(publisher.calls) == 1  # only the pre-existing heparin escalation


def _pending_doses_left() -> list[dict[str, Any]]:
    return routing_app._pending_doses()


def test_scan_window_flags_only_insulin_and_heparin_for_routing() -> None:
    result = get_doses_due()

    assert result["snapshot_ts"] == "2024-01-15T02:05:00Z"
    assert result["window_end"] == "2024-01-15T03:05:00Z"
    due_meds = {dose["med"] for dose in result["doses"]}
    assert "Metoprolol" not in due_meds  # due 03:10, outside the 60-minute window
    needs_routing = {
        (dose["room"], dose["med"]) for dose in result["doses"] if dose["needs_routing"]
    }
    assert needs_routing == {("304", "Insulin"), ("602", "Heparin")}


def test_requested_dose_lookup_ignores_window_and_case() -> None:
    result = get_doses_due(room="308", med="warfarin")

    assert [dose["due_at"] for dose in result["doses"]] == ["2024-01-15T03:30:00Z"]
    assert result["doses"][0]["home_cabinet"] == "Unit 4 cabinet"


def test_doses_show_existing_delivery_and_runner_workload() -> None:
    assert [runner["runner_id"] for runner in get_runners()] == ["N005", "N006"]

    create_routing_delivery(
        room="304",
        med="insulin",
        due_at=INSULIN_DUE_AT,
        from_location="Unit 6 cabinet",
        priority="critical",
        runner_id="N005",
    )

    insulin = get_doses_due(room="304", med="Insulin")["doses"][0]
    assert insulin["existing_delivery"]["runner_id"] == "N005"
    runners = get_runners()
    assert runners[0]["runner_id"] == "N006"  # least busy first
    assert {r["runner_id"]: r["workload"] for r in runners} == {"N005": 1, "N006": 0}
    assert all("email" not in runner and "phone" not in runner for runner in runners)


def test_request_id_matches_shared_builder_for_any_casing() -> None:
    delivery = create_routing_delivery(
        room="304",
        med="INSULIN",
        due_at="2024-01-14T18:20:00-08:00",
        from_location="Unit 6 cabinet",
        priority="critical",
        runner_id="N005",
    )

    assert delivery["request_id"] == build_request_id("304", "Insulin", INSULIN_DUE_AT)
    assert delivery["due_at"] == INSULIN_DUE_AT


def test_unavailable_stock_escalates_and_publishes_escalation_needed() -> None:
    heparin = demo_dose("heparin")
    publisher = RecordingPublisher()
    feed = RecordingFeed()
    client = CannedBedrockClient(
        [
            tool_use("tool-1", "get_doses_due", {"room": "602", "med": "Heparin"}),
            tool_use("tool-2", "find_stock", {"med": "Heparin"}),
            tool_use(
                "tool-3",
                "escalate",
                {
                    "reason": "No positive Heparin stock is available.",
                    "room": heparin["room"],
                    "to_role": "charge",
                    "med": "Heparin",
                },
            ),
            final_text("Heparin could not be routed; charge nurse escalated."),
        ]
    )

    result = handle_routing_event(
        med_requested_event(med="Heparin", room="602"),
        model_id="fake-model",
        bedrock_client=client,
        feed_logger=feed,
        event_publisher=publisher,
    )

    assert "escalated" in result["reply"]
    stored = list(escalation._ESCALATION_STORE.values())
    assert len(stored) == 1
    assert stored[0]["source_agent"] == "routing_agent"
    assert stored[0]["room"] == "602"
    # CLAUDE.md §6: EscalationNeeded {escalation_id, reason, room, to_role}.
    assert publisher.calls == [
        (
            "EscalationNeeded",
            "routing_agent",
            {
                "escalation_id": stored[0]["escalation_id"],
                "reason": "No positive Heparin stock is available.",
                "room": "602",
                "to_role": "charge",
            },
        )
    ]
    assert "event_published" in feed.actions()
    assert not deliveries.list_deliveries()


def test_repeat_escalation_for_same_dose_is_not_republished() -> None:
    publisher = RecordingPublisher()
    routing_escalate = build_tool_impls(RecordingFeed(), publisher)["escalate"]

    first = routing_escalate(reason="No stock.", room="602", med="Heparin")
    second = routing_escalate(reason="Still no stock.", room="602", med="heparin")

    assert second["duplicate"] is True
    assert second["escalation_id"] == first["escalation_id"]
    assert len(publisher.calls) == 1
    assert len(escalation._ESCALATION_STORE) == 1
    dose = get_doses_due(room="602", med="Heparin")["doses"][0]
    assert dose["open_escalation_id"] == first["escalation_id"]


def test_publish_failure_keeps_escalation_and_is_audited() -> None:
    feed = RecordingFeed()
    publisher = RecordingPublisher(error=RuntimeError("bus down"))
    routing_escalate = build_tool_impls(feed, publisher)["escalate"]

    result = routing_escalate(reason="No stock.", room="602", med="Heparin")

    assert result["event_published"] is False
    assert len(escalation._ESCALATION_STORE) == 1
    assert "event_publish_failed" in feed.actions()


def test_unsupported_or_incomplete_event_is_rejected() -> None:
    with pytest.raises(RoutingEventError, match="Unsupported"):
        handle_routing_event(
            {"detail-type": "UnknownEvent", "detail": {}},
            model_id="fake-model",
            bedrock_client=CannedBedrockClient([]),
            feed_logger=RecordingFeed(),
        )

    with pytest.raises(RoutingEventError, match="request_id"):
        handle_routing_event(
            {"detail-type": "MedRequested", "detail": {"room": "304"}},
            model_id="fake-model",
            bedrock_client=CannedBedrockClient([]),
            feed_logger=RecordingFeed(),
        )


def test_model_cannot_invent_stock_location_runner_or_dose() -> None:
    args = {
        "room": "304",
        "med": "Insulin",
        "due_at": INSULIN_DUE_AT,
        "priority": "critical",
    }

    with pytest.raises(RoutingToolError, match="was not returned"):
        create_routing_delivery(**args, from_location="Central Pharmacy", runner_id="N005")

    with pytest.raises(RoutingToolError, match="runner_id"):
        create_routing_delivery(
            **args, from_location="Unit 6 cabinet", runner_id="invented-runner"
        )

    # A nurse (not a runner) cannot be assigned a delivery.
    with pytest.raises(RoutingToolError, match="runner_id"):
        create_routing_delivery(**args, from_location="Unit 6 cabinet", runner_id="N002")

    with pytest.raises(RoutingToolError, match="due_at does not match"):
        create_routing_delivery(
            **{**args, "due_at": "2024-01-15T09:00:00Z"},
            from_location="Unit 6 cabinet",
            runner_id="N005",
        )

    with pytest.raises(RoutingToolError, match="runner_id"):
        send_email(runner_id="invented-runner", subject="Delivery", body="Route it.")


def test_send_email_live_path_uses_ses_with_resolved_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, Any]] = []

    def fake_notify(to: str, subject: str, body: str) -> NotifyResult:
        sent.append({"to": to, "subject": subject, "body": body})
        # shared/notify.send_email returns a NotifyResult (the alert agent's contract).
        return NotifyResult(to=to, subject=subject, status="sent", delivered_to=to, message_id="m-1")

    monkeypatch.setattr(routing_app, "notify_send_email", fake_notify)
    monkeypatch.setattr(
        routing_app,
        "_on_shift_runners",
        lambda: {"N006": {"nurse_id": "N006", "email": "priya@wardline.example"}},
    )

    result = send_email(runner_id="N006", subject="Urgent", body="Room 304 insulin.")

    assert sent == [
        {"to": "priya@wardline.example", "subject": "Urgent", "body": "Room 304 insulin."}
    ]
    assert result == {
        "status": "sent",
        "runner_id": "N006",
        "subject": "Urgent",
        "message_id": "m-1",
    }


def test_runner_notify_dry_run_keeps_runner_email_off_ses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Nurse alerts send for real, but runner dispatch emails are only logged.
    monkeypatch.setenv("NOTIFY_DRY_RUN", "0")
    monkeypatch.setenv("RUNNER_NOTIFY_DRY_RUN", "1")
    calls: list[dict[str, Any]] = []

    def fake_notify(to: str, subject: str, body: str, *, dry_run: bool | None = None) -> NotifyResult:
        calls.append({"to": to, "dry_run": dry_run})
        return NotifyResult(to=to, subject=subject, status="dry_run", delivered_to=to)

    monkeypatch.setattr(routing_app, "notify_send_email", fake_notify)

    result = send_email(runner_id="N005", subject="Urgent", body="Room 304 insulin.")

    assert calls == [{"to": "marcus.webb@example.com", "dry_run": True}]
    assert result["status"] == "dry_run"


def test_live_email_without_address_on_file_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.agents.routing_agent.app as routing_app

    monkeypatch.setenv("NOTIFY_DRY_RUN", "0")
    # data/nurses.json now gives every runner an email, so drop it to test the guard.
    runners = {
        runner_id: {k: v for k, v in record.items() if k != "email"}
        for runner_id, record in routing_app._on_shift_runners().items()
    }
    monkeypatch.setattr(routing_app, "_on_shift_runners", lambda: runners)

    with pytest.raises(RoutingToolError, match="No email address on file"):
        send_email(runner_id="N005", subject="Urgent", body="Room 304 insulin.")
