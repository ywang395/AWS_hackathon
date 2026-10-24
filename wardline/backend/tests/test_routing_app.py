"""Local integration tests for the routing agent (fake Bedrock, seed data)."""

from __future__ import annotations

import json
from typing import Any

import pytest

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
    assert printed["to"] == "n005@wardline.example"
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

    def fake_notify(to: str, subject: str, body: str) -> dict[str, Any]:
        sent.append({"to": to, "subject": subject, "body": body})
        return {"status": "sent", "to": to, "subject": subject, "message_id": "m-1"}

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


def test_live_email_without_address_on_file_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTIFY_DRY_RUN", "0")

    with pytest.raises(RoutingToolError, match="No email address on file"):
        send_email(runner_id="N005", subject="Urgent", body="Room 304 insulin.")
