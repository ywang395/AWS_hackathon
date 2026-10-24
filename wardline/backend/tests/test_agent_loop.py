"""Unit tests for the shared Bedrock Converse agent loop."""

from __future__ import annotations

import json
import inspect
from copy import deepcopy
from typing import Any

import pytest

from backend.shared.agent_loop import (
    AgentConfigurationError,
    AgentFeedEvent,
    AgentFeedError,
    AgentLoop,
    AgentStepLimitError,
    BedrockInvocationError,
    BedrockResponseError,
    run_agent,
)
from backend.tests.helpers import RecordingFeed


class FakeBedrockClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = deepcopy(responses)
        self.requests: list[dict[str, Any]] = []

    def converse(self, **request: Any) -> dict[str, Any]:
        self.requests.append(deepcopy(request))
        if not self.responses:
            raise AssertionError("Fake Bedrock client has no response left.")
        return deepcopy(self.responses.pop(0))


def tool_use_response(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_use_id: str = "tool-1",
) -> dict[str, Any]:
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": tool_use_id,
                            "name": tool_name,
                            "input": tool_input,
                        }
                    }
                ],
            }
        },
        "stopReason": "tool_use",
    }


def final_response(text: str) -> dict[str, Any]:
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": text}],
            }
        },
        "stopReason": "end_turn",
    }


TOOL_SPECS = [
    {
        "toolSpec": {
            "name": "find_stock",
            "description": "Find medication stock.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {"med": {"type": "string"}},
                    "required": ["med"],
                }
            },
        }
    }
]


def run_with_dependencies(
    system_prompt: str,
    tools: list[dict[str, Any]],
    user_input: str,
    tool_impls: dict[str, Any],
    max_steps: int = 8,
    *,
    model_id: str = "test-model",
    bedrock_client: Any,
    feed_logger: Any,
    agent_name: str = "agent",
) -> str:
    return AgentLoop(
        model_id=model_id,
        bedrock_client=bedrock_client,
        feed_logger=feed_logger,
        agent_name=agent_name,
    ).run(system_prompt, tools, user_input, tool_impls, max_steps)


def test_run_agent_public_signature_matches_project_contract() -> None:
    parameters = inspect.signature(run_agent).parameters

    assert list(parameters) == [
        "system_prompt",
        "tools",
        "user_input",
        "tool_impls",
        "max_steps",
    ]
    assert parameters["max_steps"].default == 8


def test_run_agent_executes_tool_and_returns_final_text() -> None:
    client = FakeBedrockClient(
        [
            tool_use_response("find_stock", {"med": "insulin"}),
            final_response("Unit 6 has insulin."),
        ]
    )
    feed = RecordingFeed()
    calls: list[dict[str, Any]] = []

    def find_stock(med: str) -> dict[str, str]:
        calls.append({"med": med})
        return {"location": "Unit 6"}

    result = run_with_dependencies(
        "Use only the supplied tools.",
        TOOL_SPECS,
        "Where is insulin?",
        {"find_stock": find_stock},
        model_id="test-model",
        bedrock_client=client,
        feed_logger=feed,
        agent_name="routing",
    )

    assert result == "Unit 6 has insulin."
    assert calls == [{"med": "insulin"}]
    assert len(client.requests) == 2

    first_request = client.requests[0]
    assert first_request["modelId"] == "test-model"
    assert first_request["system"] == [{"text": "Use only the supplied tools."}]
    assert first_request["toolConfig"] == {"tools": TOOL_SPECS}

    second_request = client.requests[1]
    assert second_request["messages"][0]["role"] == "user"
    assert second_request["messages"][1]["role"] == "assistant"
    assert second_request["messages"][2]["role"] == "user"

    tool_result = second_request["messages"][2]["content"][0]["toolResult"]
    assert tool_result["toolUseId"] == "tool-1"
    assert tool_result["status"] == "success"
    assert json.loads(tool_result["content"][0]["text"]) == {
        "location": "Unit 6"
    }

    assert [event.action for event in feed.events] == [
        "agent_started",
        "tool_requested",
        "tool_completed",
        "agent_completed",
    ]
    assert all(event.agent == "routing" for event in feed.events)


def test_run_agent_omits_tool_config_when_no_tools_are_supplied() -> None:
    client = FakeBedrockClient([final_response("No tool was needed.")])

    result = run_with_dependencies(
        "Answer briefly.",
        [],
        "Hello",
        {},
        model_id="test-model",
        bedrock_client=client,
        feed_logger=RecordingFeed(),
    )

    assert result == "No tool was needed."
    assert "toolConfig" not in client.requests[0]


def test_unknown_tool_is_returned_to_model_as_error_result() -> None:
    client = FakeBedrockClient(
        [
            tool_use_response("missing_tool", {"value": 1}),
            final_response("The requested tool is unavailable."),
        ]
    )
    feed = RecordingFeed()

    result = run_with_dependencies(
        "Use tools when necessary.",
        TOOL_SPECS,
        "Try a tool.",
        {},
        model_id="test-model",
        bedrock_client=client,
        feed_logger=feed,
    )

    assert result == "The requested tool is unavailable."
    tool_result = client.requests[1]["messages"][2]["content"][0]["toolResult"]
    assert tool_result["status"] == "error"
    assert "not available" in tool_result["content"][0]["text"]
    assert [event.action for event in feed.events] == [
        "agent_started",
        "tool_requested",
        "tool_failed",
        "agent_completed",
    ]


def test_tool_exception_is_returned_to_model_as_error_result() -> None:
    client = FakeBedrockClient(
        [
            tool_use_response("find_stock", {"med": "heparin"}),
            final_response("No heparin was found."),
        ]
    )

    def find_stock(med: str) -> dict[str, str]:
        raise RuntimeError(f"Lookup failed for {med}")

    result = run_with_dependencies(
        "Use tools when necessary.",
        TOOL_SPECS,
        "Find heparin.",
        {"find_stock": find_stock},
        model_id="test-model",
        bedrock_client=client,
        feed_logger=RecordingFeed(),
    )

    assert result == "No heparin was found."
    tool_result = client.requests[1]["messages"][2]["content"][0]["toolResult"]
    assert tool_result["status"] == "error"
    assert "Tool 'find_stock' failed" in tool_result["content"][0]["text"]
    assert "Lookup failed for heparin" not in tool_result["content"][0]["text"]


def test_step_limit_stops_before_executing_extra_tool() -> None:
    client = FakeBedrockClient(
        [tool_use_response("find_stock", {"med": "insulin"})]
    )
    feed = RecordingFeed()
    calls = 0

    def find_stock(med: str) -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"med": med}

    with pytest.raises(AgentStepLimitError, match="max_steps=1"):
        run_with_dependencies(
            "Use tools when necessary.",
            TOOL_SPECS,
            "Find insulin.",
            {"find_stock": find_stock},
            max_steps=1,
            model_id="test-model",
            bedrock_client=client,
            feed_logger=feed,
        )

    assert calls == 0
    assert [event.action for event in feed.events] == [
        "agent_started",
        "step_limit_reached",
    ]
    assert feed.events[1].detail["requested_tools"] == ["find_stock"]


def test_final_response_without_text_is_rejected() -> None:
    client = FakeBedrockClient(
        [
            {
                "output": {
                    "message": {
                        "role": "assistant",
                        "content": [],
                    }
                },
                "stopReason": "end_turn",
            }
        ]
    )

    with pytest.raises(BedrockResponseError, match="without a text response"):
        run_with_dependencies(
            "Answer briefly.",
            [],
            "Hello",
            {},
            model_id="test-model",
            bedrock_client=client,
            feed_logger=RecordingFeed(),
        )


def test_invalid_step_limit_is_rejected_before_client_call() -> None:
    client = FakeBedrockClient([])

    with pytest.raises(AgentConfigurationError, match="at least 1"):
        run_with_dependencies(
            "Answer briefly.",
            [],
            "Hello",
            {},
            max_steps=0,
            model_id="test-model",
            bedrock_client=client,
            feed_logger=RecordingFeed(),
        )


def test_client_failure_is_wrapped_with_agent_loop_error() -> None:
    class FailingClient:
        def converse(self, **request: Any) -> dict[str, Any]:
            raise RuntimeError("network unavailable")

    with pytest.raises(BedrockInvocationError, match="client failed"):
        run_with_dependencies(
            "Answer briefly.",
            [],
            "Hello",
            {},
            model_id="test-model",
            bedrock_client=FailingClient(),
            feed_logger=RecordingFeed(),
        )


def test_feed_failure_stops_agent_execution() -> None:
    class FailingFeed:
        def log(self, event: AgentFeedEvent) -> None:
            raise RuntimeError("feed unavailable")

    client = FakeBedrockClient([final_response("Should not be returned.")])

    with pytest.raises(AgentFeedError, match="Unable to record AgentFeed"):
        run_with_dependencies(
            "Answer briefly.",
            [],
            "Hello",
            {},
            model_id="test-model",
            bedrock_client=client,
            feed_logger=FailingFeed(),
        )

    assert client.requests == []
