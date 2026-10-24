"""CLAUDE.md section 10 (M3) demo cases owned by the nurse + alert workstream,
run end to end on the real seed data in data/*.json.

  1. An on-shift nurse doesn't ack -> resend -> escalation to the charge nurse
  2. Double-dose caught from the shared DowntimeLog -> STOP with who/when
  3. Nurse A asks about Nurse B's patient -> refused

Seed roster (data/nurses.json): N001 Lin (charge), N002 Alisher, N003 Salih,
N004 Sunny are the four on-shift nurses. Patients P401-P404 are Lin's,
P405-P408 Alisher's, P409-P412 Salih's, P413-P416 Sunny's.

Bedrock is the canned fake from the routing agent, so these show what the
code guarantees no matter what the model does: the STOP and the refusal are
enforced by tools, not by the prompt.
"""

import json

import pytest

from backend.agents.alert_agent import app as alert_app
from backend.agents.nurse_agent import app as nurse
from backend.agents.routing_agent.fake_bedrock import CannedBedrockClient, final_text, tool_use
from backend.shared import auth
from backend.tools import downtime_log


@pytest.fixture(autouse=True)
def local_backend(monkeypatch):
    monkeypatch.setenv("DATA_BACKEND", "json")
    monkeypatch.setenv("NOTIFY_DRY_RUN", "1")


def _deps(tokens, log_store, client=None, record_ack=None):
    return nurse.ChatDeps(
        token_store=tokens,
        log_store=log_store,
        history=nurse.InMemoryChatHistory(),
        publish_event=lambda *args: None,
        record_ack=record_ack,
        load_deliveries=list,
        bedrock_client=client,
        model_id="fake-model",
    )


def _last_tool_result(client: CannedBedrockClient) -> dict:
    """The tool result the model saw on its final turn."""
    block = client.requests[-1]["messages"][-1]["content"][0]["toolResult"]
    return {"status": block["status"], "text": block["content"][0]["text"]}


def test_a_nurse_does_not_ack_and_is_escalated_to_the_charge_nurse(capsys):
    tokens, acks = auth.InMemoryTokenStore(), alert_app.InMemoryAckStore()
    staff = alert_app._load_nurses()  # data/nurses.json
    deps = _deps(tokens, downtime_log.InMemoryLogStore(),
                 record_ack=lambda nurse_id: alert_app.record_ack(nurse_id, store=acks))

    alert_app.run_ack_sweep(store=acks, nurses=staff, token_store=tokens)  # OutageDetected

    # Lin (charge), Salih and Sunny open their links; Alisher (N002) never does.
    for nurse_id in ("N001", "N003", "N004"):
        nurse.handle_chat({"token": auth.create_token(nurse_id, store=tokens), "pin": "1234", "message": ""}, deps)

    for _ in range(alert_app.DEFAULT_MAX_ATTEMPTS):  # AckCheck every 5 minutes
        outcomes = {o["nurse_id"]: o for o in alert_app.run_ack_sweep(store=acks, nurses=staff, token_store=tokens)}

    assert outcomes["N002"]["action"] == "escalated"
    escalation = outcomes["N002"]["escalation"]
    assert (escalation["nurse_id"], escalation["to_role"], escalation["source_agent"]) == ("N002", "charge", "alert_agent")
    assert {outcomes[n]["action"] for n in ("N001", "N003", "N004")} == {"none"}

    emails = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith('{"event": "email"')]
    to_absent = [e for e in emails if e["to"] == "alisher.nurmatov@sjsu.edu"]
    assert len(to_absent) == alert_app.DEFAULT_MAX_ATTEMPTS  # first alert + 2 reminders, then escalation
    assert "secure" not in to_absent[0]["subject"].lower() and "token=" in to_absent[0]["body"]


def test_double_dose_is_stopped_from_the_shared_log():
    tokens, log_store = auth.InMemoryTokenStore(), downtime_log.InMemoryLogStore()
    # Room 304 = Eleanor Whitmore (P304), insulin, assigned to Lin (N001).
    give_insulin = tool_use("t1", "log_event", {"room": "304", "event_type": "med_given", "detail": "Insulin 8 units SubQ"})
    client = CannedBedrockClient([
        give_insulin, final_text("Logged insulin for 304."),
        give_insulin, final_text("STOP: insulin was already given."),
    ])
    deps = _deps(tokens, log_store, client)

    # Two separate links/devices: the STOP comes from the shared log, not chat memory.
    for _ in range(2):
        token = auth.create_token("N001", store=tokens)
        nurse.handle_chat({"token": token, "pin": "1234", "message": "I gave 304 their insulin."}, deps)

    assert len(log_store.entries_for("P304")) == 1  # the repeat was never written
    result = json.loads(_last_tool_result(client)["text"])
    assert result["status"] == "stop"
    assert result["given_by"] == "Lin Wang"
    assert result["message"].startswith("STOP: Insulin was already given to room 304 by Lin Wang")


def test_other_nurses_patient_is_refused_without_leaking_the_chart():
    tokens = auth.InMemoryTokenStore()
    client = CannedBedrockClient([
        tool_use("t1", "get_patient", {"room": "309"}),  # Carmen Ruiz, assigned to Salih (N003)
        final_text("Room 309 is not one of your patients."),
    ])
    deps = _deps(tokens, downtime_log.InMemoryLogStore(), client)

    response = nurse.handle_chat(
        {"token": auth.create_token("N002", store=tokens), "pin": "1234", "message": "What are 309's allergies?"}, deps,
    )

    result = _last_tool_result(client)
    assert result["status"] == "error"
    assert "PatientAccessDenied" in result["text"]
    everything_the_model_saw = json.dumps(client.requests)
    for leaked in ("Carmen", "Ruiz", "1944-10-27"):
        assert leaked not in everything_the_model_saw
    assert {p["room"] for p in response["patients"]} == {"305", "306", "307", "308"}