"""
Cross-module integration checks: verify the independently-built pieces
actually fit together and honor the contracts in CLAUDE.md.

These tests are intentionally READ-ONLY against product code -- they only
call public functions and assert on contracts (CLAUDE.md sections 5, 6,
8). A failure here is a report of an incompatibility, not a signal to
edit the module under test.
"""

import inspect
import json
from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parents[2] / "data"


# ---------------------------------------------------------------------------
# §6 event contract -- events.py must match CLAUDE.md's bus + detail types
# ---------------------------------------------------------------------------

def test_events_allowed_sources_match_claude_md_section_6():
    from backend.shared.events import ALLOWED_SOURCES_BY_DETAIL_TYPE as allowed

    assert allowed["OutageDetected"] == frozenset({"control"})
    assert allowed["MedRequested"] == frozenset({"nurse_agent"})
    assert allowed["EscalationNeeded"] == frozenset({"routing_agent", "alert_agent"})
    assert allowed["AckCheck"] == frozenset({"scheduler"})
    assert allowed["SystemRestored"] == frozenset({"control"})


def test_events_default_bus_is_wardline():
    from backend.shared.events import DEFAULT_EVENT_BUS_NAME

    assert DEFAULT_EVENT_BUS_NAME == "wardline"


# ---------------------------------------------------------------------------
# §8 escalate() -- the shared tool the alert AND routing agents both call
# ---------------------------------------------------------------------------

def test_alert_agent_escalate_matches_tools_escalation_signature():
    """
    alert_agent/app.py calls the shared escalate() with keyword args
    (reason, nurse_id, room, to_role, source_agent). This test checks the
    REAL tools.escalation.escalate accepts exactly that call.
    """
    from backend.tools.escalation import escalate

    sig = inspect.signature(escalate)
    params = sig.parameters
    # alert_agent passes nurse_id -- confirm the shared tool accepts it.
    assert "nurse_id" in params, (
        "tools.escalation.escalate has no 'nurse_id' param, but "
        "alert_agent/app.py calls escalate(..., nurse_id=...). "
        f"Actual params: {list(params)}"
    )


def test_alert_agent_escalate_call_actually_works_against_shared_tool():
    """
    End-to-end: import alert_agent (which binds to the real
    tools.escalation.escalate now that it exists) and trigger an
    escalation the way the Lambda handler does. If the signatures are
    incompatible, this raises TypeError.
    """
    from backend.agents.alert_agent import app as alert_app

    result = alert_app.escalate(
        reason="integration check",
        nurse_id="N002",
        room="304",
        to_role="charge",
        source_agent="alert_agent",
    )
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# §5 data <-> tools wiring
# ---------------------------------------------------------------------------

def test_stock_reads_the_same_inventory_demo_cases():
    from backend.tools.stock import find_stock

    insulin = find_stock("Insulin")
    # Insulin must be sourceable somewhere (Unit 6 / Central) even though
    # Unit 4 is empty -- the routing demo depends on it.
    assert any(row.get("qty", 0) > 0 for row in insulin), insulin

    heparin = find_stock("Heparin")
    # Heparin is nowhere -> every row qty 0 (or no rows). Either way, no stock.
    assert all(row.get("qty", 0) == 0 for row in heparin), heparin


def test_patients_and_nurses_reference_each_other_consistently():
    nurses = json.loads((DATA / "nurses.json").read_text(encoding="utf-8"))
    patients = json.loads((DATA / "patients.json").read_text(encoding="utf-8"))

    nurse_ids = {n["nurse_id"] for n in nurses}
    # Every patient's assigned nurse must exist in nurses.json.
    for p in patients:
        assert p["assigned_nurse_id"] in nurse_ids, p["assigned_nurse_id"]


# ---------------------------------------------------------------------------
# §8 routing agent tool set -- CLAUDE.md lists send_email (SES) for runner dispatch
# ---------------------------------------------------------------------------

def test_routing_agent_tool_names_cover_claude_md_section_8():
    """
    CLAUDE.md section 8 routing tools: get_doses_due, find_stock,
    get_runners, create_delivery, send_email, escalate.
    """
    from backend.agents.routing_agent.tool_specs import ROUTING_TOOL_NAMES

    expected = {
        "get_doses_due",
        "find_stock",
        "get_runners",
        "create_delivery",
        "send_email",
        "escalate",
    }
    missing = expected - set(ROUTING_TOOL_NAMES)
    assert not missing, f"routing agent missing CLAUDE.md section 8 tools: {missing}"