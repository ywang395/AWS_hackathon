"""Contract checks for the routing agent system prompt."""

from __future__ import annotations

from pathlib import Path

PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "agents" / "routing_agent" / "prompt.md"
)


def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def test_prompt_defines_required_tools_and_safety_rules() -> None:
    prompt = load_prompt()

    for tool_name in [
        "get_doses_due",
        "find_stock",
        "get_runners",
        "create_delivery",
        "send_sms",
        "escalate",
    ]:
        assert tool_name in prompt

    assert "Never substitute one medication for another." in prompt
    assert "Never calculate, change, or recommend a dose." in prompt
    assert "create_delivery derives request_id" in prompt
    assert "Call `get_doses_due` and resolve the matching patient" in prompt
    assert "<!-- CRITICAL SAFETY:" in prompt


def test_prompt_covers_insulin_and_heparin_demo_cases() -> None:
    prompt = load_prompt()

    assert "Unit 4 has no insulin but Unit 6 has positive stock" in prompt
    assert "heparin has no positive stock anywhere" in prompt
    assert "escalate to the charge nurse" in prompt
    assert "send_email" not in prompt
