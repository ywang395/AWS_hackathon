"""Test doubles shared by the backend test modules."""

from __future__ import annotations

from typing import Any

from backend.shared.agent_loop import AgentFeedEvent


class RecordingFeed:
    """AgentFeed logger that keeps events in memory; can be told to fail."""

    def __init__(self, error: Exception | None = None) -> None:
        self.events: list[AgentFeedEvent] = []
        self.error = error

    def log(self, event: AgentFeedEvent) -> None:
        if self.error is not None:
            raise self.error
        self.events.append(event)

    def actions(self) -> list[str]:
        return [event.action for event in self.events]

    def tools_called(self) -> list[str]:
        return [
            event.detail["tool"]
            for event in self.events
            if event.action == "tool_requested"
        ]


class RecordingPublisher:
    """put_event stand-in that records calls; can be told to fail."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.error = error

    def __call__(self, detail_type: str, source: str, detail: dict[str, Any]) -> None:
        self.calls.append((detail_type, source, dict(detail)))
        if self.error is not None:
            raise self.error
