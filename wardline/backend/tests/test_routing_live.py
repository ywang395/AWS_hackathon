r"""Opt-in live tests: routing agent against real Amazon Bedrock and seed data.

Skipped unless WARDLINE_LIVE_BEDROCK=1 and MODEL_ID are set, plus AWS
credentials (e.g. AWS_BEARER_TOKEN_BEDROCK). Email and events stay in dry-run.

    $env:WARDLINE_LIVE_BEDROCK="1"
    $env:MODEL_ID="us.anthropic.claude-haiku-4-5-20251001-v1:0"
    $env:AWS_REGION="us-east-1"
    .\.venv\Scripts\python.exe -m pytest wardline\backend\tests\test_routing_live.py
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from backend.agents.routing_agent import app as routing_app
from backend.agents.routing_agent.app import (
    _demo_event,
    demo_dose,
    handle_routing_event,
    run_routing_scan,
)
from backend.shared.agent_loop import AgentFeedEvent
from backend.tools import deliveries, escalation

pytestmark = pytest.mark.skipif(
    os.getenv("WARDLINE_LIVE_BEDROCK") != "1" or not os.getenv("MODEL_ID"),
    reason="Set WARDLINE_LIVE_BEDROCK=1 and MODEL_ID to call real Bedrock.",
)

RUNNER_IDS = {"N005", "N006"}  # Marcus, Priya in data/nurses.json


class RecordingFeed:
    def __init__(self) -> None:
        self.events: list[AgentFeedEvent] = []

    def log(self, event: AgentFeedEvent) -> None:
        self.events.append(event)

    def tools_called(self) -> list[str]:
        return [
            event.detail["tool"]
            for event in self.events
            if event.action == "tool_requested"
        ]


class RecordingPublisher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, detail_type: str, source: str, detail: dict[str, Any]) -> None:
        self.calls.append((detail_type, source, dict(detail)))


@pytest.fixture(autouse=True)
def isolated_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFY_DRY_RUN", "1")
    monkeypatch.setenv("DRY_RUN", "1")
    deliveries.reset_deliveries()
    escalation.reset_escalations()
    routing_app.reset_routing_state()
    yield
    deliveries.reset_deliveries()
    escalation.reset_escalations()
    routing_app.reset_routing_state()


def test_live_insulin_routes_from_unit_6() -> None:
    feed = RecordingFeed()

    result = handle_routing_event(_demo_event("insulin"), feed_logger=feed)

    assert result["event_type"] == "MedRequested"
    assert result["reply"].startswith("Records as of")
    stored = deliveries.list_deliveries()
    assert len(stored) == 1
    assert stored[0]["from_location"] == "Unit 6 cabinet"
    assert stored[0]["to_room"] == "304"
    assert stored[0]["due_at"] == demo_dose("insulin")["due_at"]
    assert stored[0]["runner_id"] in RUNNER_IDS
    assert "send_email" in feed.tools_called()
    assert not escalation._ESCALATION_STORE
    assert not any(e.action == "tool_failed" for e in feed.events)


def test_live_heparin_escalates_and_publishes() -> None:
    feed = RecordingFeed()
    publisher = RecordingPublisher()

    result = handle_routing_event(
        _demo_event("heparin"), feed_logger=feed, event_publisher=publisher
    )

    assert result["event_type"] == "MedRequested"
    assert not deliveries.list_deliveries()
    stored = list(escalation._ESCALATION_STORE.values())
    assert len(stored) == 1
    assert stored[0]["room"] == "602"
    assert stored[0]["to_role"] == "charge"
    assert [call[0] for call in publisher.calls] == ["EscalationNeeded"]
    assert "create_delivery" not in feed.tools_called()


def test_live_scan_routes_insulin_escalates_heparin_and_is_idempotent() -> None:
    publisher = RecordingPublisher()

    first = run_routing_scan(feed_logger=RecordingFeed(), event_publisher=publisher)
    second_feed = RecordingFeed()
    run_routing_scan(feed_logger=second_feed, event_publisher=publisher)

    assert first["event_type"] == "RoutingScan"
    assert {d["med"] for d in deliveries.list_deliveries()} == {"Insulin"}
    assert {e["room"] for e in escalation._ESCALATION_STORE.values()} == {"602"}
    # The repeat scan must not re-email the runner or re-alert the charge nurse.
    assert len(publisher.calls) == 1
    assert "send_email" not in second_feed.tools_called()
    assert len(escalation._ESCALATION_STORE) == 1
