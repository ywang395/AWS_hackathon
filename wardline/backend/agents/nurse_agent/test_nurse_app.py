"""Unit tests for agents/nurse_agent/app.py (POST /chat) on the JSON backend.

Bedrock is replaced by the routing agent's CannedBedrockClient, and every
store is an in-memory instance passed through ChatDeps, so nothing here
needs AWS and no state leaks between tests.
"""

import json

import pytest

from backend.agents.nurse_agent import app as nurse
from backend.agents.nurse_agent.tool_specs import NURSE_TOOL_NAMES, NURSE_TOOL_SPECS
from backend.agents.routing_agent.fake_bedrock import CannedBedrockClient, final_text, tool_use
from backend.shared import auth
from backend.shared.events import put_event
from backend.tools import deliveries, downtime_log
from backend.tools.patients import PatientAccessDenied


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("DATA_BACKEND", "json")
    tokens = auth.InMemoryTokenStore()
    events, acks = [], []

    def publish(detail_type, source, detail):
        put_event(detail_type, source, detail, dry_run=True)  # enforces the section 6 contract
        events.append((detail_type, source, dict(detail)))

    def make(responses=(), delivery_rows=()):
        return nurse.ChatDeps(
            token_store=tokens,
            log_store=downtime_log.InMemoryLogStore(),
            history=nurse.InMemoryChatHistory(),
            publish_event=publish,
            record_ack=acks.append,
            load_deliveries=lambda: list(delivery_rows),
            bedrock_client=CannedBedrockClient(list(responses)),
            model_id="fake-model",
        )

    class Env:
        pass

    e = Env()
    e.deps, e.events, e.acks = make, events, acks
    e.token = lambda nurse_id="N002": auth.create_token(nurse_id, store=tokens)
    return e


def test_empty_message_returns_the_brief_for_own_patients_only(env):
    deps = env.deps()
    response = nurse.handle_chat({"token": env.token(), "pin": "1234", "message": ""}, deps)

    assert [b["room"] for b in response["brief"]] == ["301", "302", "303", "304"]
    assert {p["assigned_nurse_id"] for p in response["patients"]} == {"N002"}
    assert response["snapshot_ts"] == "2024-01-15T02:05:00Z"
    assert "Records as of 02:05 UTC" in response["reply"]
    room_304 = next(b for b in response["brief"] if b["room"] == "304")
    assert room_304["allergies"] == ["Penicillin"]
    assert room_304["critical_meds"] == ["Insulin"]
    assert env.acks == ["N002"]  # opening the link acknowledged the alert
    assert deps.bedrock_client.requests == []  # the brief never calls the model


def test_response_matches_the_section_7_contract_keys(env):
    response = nurse.handle_chat({"token": env.token(), "pin": "1234", "message": ""}, env.deps())
    assert set(response) == {"reply", "snapshot_ts", "brief", "patients", "deliveries"}


def test_wrong_pin_and_unknown_token_get_the_same_401(env):
    with pytest.raises(nurse.ChatError) as wrong_pin:
        nurse.handle_chat({"token": env.token(), "pin": "0000"}, env.deps())
    with pytest.raises(nurse.ChatError) as bad_token:
        nurse.handle_chat({"token": "made-up", "pin": "1234"}, env.deps())
    assert wrong_pin.value.status == bad_token.value.status == 401
    assert wrong_pin.value.message == bad_token.value.message
    assert env.acks == []


def test_missing_fields_are_400(env):
    with pytest.raises(nurse.ChatError) as exc:
        nurse.handle_chat({"token": env.token()}, env.deps())
    assert exc.value.status == 400


def test_deliveries_are_filtered_to_own_rooms(env):
    rows = [{"request_id": "a", "to_room": "304"}, {"request_id": "b", "to_room": "305"}]
    response = nurse.handle_chat({"token": env.token(), "pin": "1234", "message": ""}, env.deps(delivery_rows=rows))
    assert [d["request_id"] for d in response["deliveries"]] == ["a"]


def test_request_med_publishes_med_requested_with_the_delivery_request_id(env):
    tools = nurse.build_tool_impls("N002", env.deps())

    result = tools["request_med"](room="304", med="insulin")

    assert result["status"] == "requested"
    (detail_type, source, detail), = env.events
    assert (detail_type, source) == ("MedRequested", "nurse_agent")
    assert set(detail) == {"request_id", "room", "med", "requested_by"}  # section 6 detail shape
    assert detail["requested_by"] == "N002"  # bound in code, not chosen by the model
    assert detail["request_id"] == deliveries.build_request_id("304", "Insulin", "2024-01-15T02:20:00Z")
    assert detail["request_id"] == "304-insulin-2024-01-15T02:20:00Z"


def test_request_med_refuses_other_rooms_and_unordered_meds(env):
    tools = nurse.build_tool_impls("N002", env.deps())
    with pytest.raises(PatientAccessDenied):
        tools["request_med"](room="305", med="Amlodipine")
    assert tools["request_med"](room="304", med="Heparin")["status"] == "not_in_record"
    assert env.events == []


def test_tool_specs_match_impls_and_never_take_a_nurse_id(env):
    assert set(nurse.build_tool_impls("N002", env.deps())) == NURSE_TOOL_NAMES
    for spec in NURSE_TOOL_SPECS:
        assert "nurse_id" not in spec["toolSpec"]["inputSchema"]["json"]["properties"]


def test_model_cannot_slip_in_a_nurse_id(env):
    tools = nurse.build_tool_impls("N002", env.deps())
    with pytest.raises(TypeError):
        tools["get_patient"](room="305", nurse_id="N003")


def test_chat_reply_always_carries_the_snapshot_time_and_keeps_history(env):
    deps = env.deps(responses=[final_text("Room 304 is allergic to penicillin."), final_text("Yes.")])
    token = env.token()

    first = nurse.handle_chat({"token": token, "pin": "1234", "message": "Allergies in 304?"}, deps)
    nurse.handle_chat({"token": token, "pin": "1234", "message": "Anything else?"}, deps)

    assert first["reply"].endswith("(Records as of 02:05 UTC)")  # appended by code
    assert "brief" not in first
    second_prompt = deps.bedrock_client.requests[1]["messages"][0]["content"][0]["text"]
    assert "Earlier in this conversation:" in second_prompt
    assert "Allergies in 304?" in second_prompt
    assert "02:05 UTC" in deps.bedrock_client.requests[0]["system"][0]["text"]


def test_bedrock_failure_degrades_to_a_safe_reply(env):
    deps = env.deps(responses=[])  # the fake raises on the first call
    response = nurse.handle_chat({"token": env.token(), "pin": "1234", "message": "Hi"}, deps)
    assert "assistant is unavailable" in response["reply"]
    assert len(response["patients"]) == 4


def test_http_handler_maps_errors_to_status_codes(env, monkeypatch):
    deps = env.deps()
    ok = nurse.http_handler({"body": json.dumps({"token": env.token(), "pin": "1234", "message": ""})}, deps=deps)
    assert ok["statusCode"] == 200
    assert json.loads(ok["body"])["snapshot_ts"] == "2024-01-15T02:05:00Z"
    assert nurse.http_handler({"body": "{not json"}, deps=deps)["statusCode"] == 400
    denied = nurse.http_handler({"body": json.dumps({"token": "x", "pin": "1"})}, deps=deps)
    assert denied["statusCode"] == 401