"""Local integration tests for the routing agent Lambda handler."""

from __future__ import annotations

import json
from typing import Any

import pytest

from backend.agents.routing_agent.app import (
    RoutingEventError,
    RoutingToolError,
    create_routing_delivery,
    get_doses_due,
    handle_routing_event,
    send_email,
)
from backend.agents.routing_agent.fake_bedrock import (
    CannedBedrockClient,
    final_text,
    insulin_demo_responses,
    tool_use,
)
from backend.shared.agent_loop import AgentFeedEvent
from backend.tools import deliveries, escalation


class RecordingFeed:
    def __init__(self) -> None:
        self.events: list[AgentFeedEvent] = []

    def log(self, event: AgentFeedEvent) -> None:
        self.events.append(event)


@pytest.fixture(autouse=True)
def reset_in_memory_stores() -> None:
    deliveries.reset_deliveries()
    escalation.reset_escalations()
    yield
    deliveries.reset_deliveries()
    escalation.reset_escalations()


def med_requested_event(
    med: str = "insulin",
    room: str = "304",
) -> dict[str, Any]:
    return {
        "detail-type": "MedRequested",
        "source": "nurse_agent",
        "detail": {
            "request_id": f"{room}-{med}-local",
            "room": room,
            "med": med,
            "requested_by": "nurse-kim",
        },
    }


def test_med_requested_runs_full_fake_bedrock_workflow(
    capsys: pytest.CaptureFixture[str],
) -> None:
    due_at = get_doses_due()[0]["due_at"]
    client = CannedBedrockClient(insulin_demo_responses(due_at))
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
    assert len(deliveries._DELIVERY_STORE) == 1
    delivery = next(iter(deliveries._DELIVERY_STORE.values()))
    assert delivery["from_location"] == "Unit 6 cabinet"
    assert delivery["runner_id"] == "marcus"

    requested_tools = [
        event.detail["tool"]
        for event in feed.events
        if event.action == "tool_requested"
    ]
    assert requested_tools == [
        "get_doses_due",
        "find_stock",
        "get_runners",
        "create_delivery",
        "send_email",
    ]
    assert "Unit 6 cabinet" in json.loads(capsys.readouterr().out)["body"]


def test_scheduled_event_runs_scan_prompt() -> None:
    client = CannedBedrockClient(
        [
            tool_use("tool-1", "get_doses_due", {}),
            final_text("Scan complete; no doses remain unrouted."),
        ]
    )

    result = handle_routing_event(
        {
            "source": "aws.scheduler",
            "detail-type": "Scheduled Event",
            "detail": {},
        },
        model_id="fake-model",
        bedrock_client=client,
        feed_logger=RecordingFeed(),
    )

    assert result["event_type"] == "RoutingScan"
    user_message = client.requests[0]["messages"][0]["content"][0]["text"]
    assert "routing scan" in user_message
    assert "next 60 minutes" in user_message


def test_unavailable_stock_uses_escalation_tool() -> None:
    client = CannedBedrockClient(
        [
            tool_use("tool-1", "get_doses_due", {}),
            tool_use("tool-2", "find_stock", {"med": "heparin"}),
            tool_use(
                "tool-3",
                "escalate",
                {
                    "reason": "No positive heparin stock is available.",
                    "room": "312",
                    "to_role": "charge",
                },
            ),
            final_text("Heparin could not be routed; charge nurse escalated."),
        ]
    )

    result = handle_routing_event(
        med_requested_event(med="heparin", room="312"),
        model_id="fake-model",
        bedrock_client=client,
        feed_logger=RecordingFeed(),
    )

    assert "escalated" in result["reply"]
    assert len(escalation._ESCALATION_STORE) == 1
    stored = next(iter(escalation._ESCALATION_STORE.values()))
    assert stored["source_agent"] == "routing_agent"
    assert stored["room"] == "312"


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


def test_model_cannot_invent_stock_location_or_runner() -> None:
    args = {
        "room": "304",
        "med": "insulin",
        "due_at": "2026-10-24T02:00:00Z",
        "priority": "critical",
    }

    with pytest.raises(RoutingToolError, match="was not returned"):
        create_routing_delivery(
            **args,
            from_location="Central pharmacy",
            runner_id="marcus",
        )

    with pytest.raises(RoutingToolError, match="runner_id"):
        create_routing_delivery(
            **args,
            from_location="Unit 6 cabinet",
            runner_id="invented-runner",
        )

    with pytest.raises(RoutingToolError, match="due_at does not match"):
        create_routing_delivery(
            **{**args, "due_at": "2026-10-24T02:00:00Z"},
            from_location="Unit 6 cabinet",
            runner_id="marcus",
        )

    with pytest.raises(RoutingToolError, match="runner_id"):
        send_email(
            runner_id="invented-runner",
            subject="Delivery",
            body="Route medication.",
        )
