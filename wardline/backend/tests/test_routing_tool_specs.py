"""Contract tests for routing-agent Bedrock tool schemas."""

from __future__ import annotations

import inspect

from backend.agents.routing_agent.tool_specs import (
    ROUTING_TOOL_NAMES,
    ROUTING_TOOL_SPECS,
)
from backend.tools.deliveries import create_delivery
from backend.tools.stock import find_stock


def tool_specs_by_name() -> dict[str, dict]:
    return {
        spec["toolSpec"]["name"]: spec["toolSpec"]
        for spec in ROUTING_TOOL_SPECS
    }


def test_routing_schema_names_match_prompt_contract() -> None:
    assert ROUTING_TOOL_NAMES == {
        "get_doses_due",
        "find_stock",
        "get_runners",
        "create_delivery",
        "send_sms",
        "escalate",
    }


def test_every_tool_uses_bedrock_object_schema() -> None:
    for spec in ROUTING_TOOL_SPECS:
        tool_spec = spec["toolSpec"]
        assert isinstance(tool_spec["name"], str)
        assert isinstance(tool_spec["description"], str)
        json_schema = tool_spec["inputSchema"]["json"]
        assert json_schema["type"] == "object"
        assert isinstance(json_schema["properties"], dict)
        assert json_schema["additionalProperties"] is False


def test_find_stock_schema_matches_implementation() -> None:
    schema = tool_specs_by_name()["find_stock"]["inputSchema"]["json"]
    signature = inspect.signature(find_stock)

    assert schema["required"] == ["med"]
    assert list(signature.parameters) == ["med"]


def test_create_delivery_schema_matches_implementation() -> None:
    schema = tool_specs_by_name()["create_delivery"]["inputSchema"]["json"]
    signature = inspect.signature(create_delivery)

    assert schema["required"] == [
        "room",
        "med",
        "due_at",
        "from_location",
        "priority",
        "runner_id",
    ]
    assert list(signature.parameters) == schema["required"]


def test_escalation_schema_excludes_server_bound_source_agent() -> None:
    schema = tool_specs_by_name()["escalate"]["inputSchema"]["json"]

    assert schema["required"] == ["reason", "room", "to_role"]
    assert "source_agent" not in schema["properties"]
