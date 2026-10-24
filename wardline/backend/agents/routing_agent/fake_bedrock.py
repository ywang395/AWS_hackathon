"""Canned Bedrock responses for local routing-agent execution."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


class CannedBedrockClient:
    """Return pre-scripted Converse responses without calling AWS."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = deepcopy(responses)
        self.requests: list[dict[str, Any]] = []

    def converse(self, **request: Any) -> dict[str, Any]:
        self.requests.append(deepcopy(request))
        if not self.responses:
            raise AssertionError("No canned Bedrock response remains.")
        return deepcopy(self.responses.pop(0))


def tool_use(
    tool_use_id: str,
    name: str,
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": tool_use_id,
                            "name": name,
                            "input": tool_input,
                        }
                    }
                ],
            }
        },
        "stopReason": "tool_use",
    }


def final_text(text: str) -> dict[str, Any]:
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": text}],
            }
        },
        "stopReason": "end_turn",
    }


def insulin_demo_responses(due_at: str) -> list[dict[str, Any]]:
    return [
        tool_use("tool-1", "get_doses_due", {}),
        tool_use("tool-2", "find_stock", {"med": "insulin"}),
        tool_use("tool-3", "get_runners", {}),
        tool_use(
            "tool-4",
            "create_delivery",
            {
                "room": "304",
                "med": "insulin",
                "due_at": due_at,
                "from_location": "Unit 6 cabinet",
                "priority": "critical",
                "runner_id": "marcus",
            },
        ),
        tool_use(
            "tool-5",
            "send_email",
            {
                "runner_id": "marcus",
                "subject": "Critical delivery: insulin to room 304",
                "body": "Pick up insulin from Unit 6 cabinet.",
            },
        ),
        final_text("Insulin routed from Unit 6 to room 304 with Marcus."),
    ]
