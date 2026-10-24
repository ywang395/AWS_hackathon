"""DATA_BACKEND=dynamodb: every switched tool against the in-memory fake.

The `seeded_dynamo` fixture (root conftest.py) loads the tables through
scripts/seed.py, so these tests also prove the seed writes items the tools
can read back, floats included.
"""

import time

import pytest

from backend.agents.alert_agent import app as alert_app
from backend.agents.nurse_agent.app import ChatDeps, DynamoChatHistory, handle_chat
from backend.shared import auth, db
from backend.tests.fake_dynamodb import FakeDynamoResource
from backend.tools import downtime_log
from backend.tools.patients import PatientAccessDenied, get_my_patients, get_patient


def test_seed_writes_every_fixture_and_floats_round_trip(seeded_dynamo):
    assert len(seeded_dynamo.Table("Nurses").items) == 12
    assert len(seeded_dynamo.Table("Patients").items) == 16
    assert len(seeded_dynamo.Table("Inventory").items) == 25
    patient_304 = db.get_item("patients", patient_id="P304")
    assert patient_304["vitals"]["temp_f"] == 99.4  # stored as Decimal, read back as float


def test_raw_float_is_rejected_like_real_boto3(fake_dynamo):
    with pytest.raises(TypeError, match="Float types"):
        fake_dynamo.Table("Patients").put_item(Item={"patient_id": "X", "temp_f": 98.6})
    db.put_item("patients", {"patient_id": "X", "temp_f": 98.6})  # db.py converts first
    assert db.get_item("patients", patient_id="X")["temp_f"] == 98.6


def test_scan_follows_pagination(monkeypatch):
    monkeypatch.setenv("DATA_BACKEND", "dynamodb")
    db.set_resource(FakeDynamoResource(page_size=2))
    try:
        for i in range(5):
            db.put_item("nurses", {"nurse_id": f"N{i}"})
        assert len(db.scan("nurses")) == 5
    finally:
        db.set_resource(None)


def test_tokens_and_pin_check_use_dynamodb_by_default(seeded_dynamo):
    token = auth.create_token("N002")  # no store= -> DynamoTokenStore
    row = seeded_dynamo.Table("Tokens").items[(token, None)]
    assert isinstance(row["expires_at"], int)  # TTL attribute must be whole epoch seconds
    assert auth.authenticate(token, "1234") == "N002"  # DynamoNurseLookup reads the seeded pin_hash
    with pytest.raises(auth.AuthError):
        auth.authenticate(token, "0000")


def test_expired_dynamodb_token_is_rejected_and_deleted(seeded_dynamo):
    token = auth.create_token("N002", ttl_seconds=-1)
    assert auth.verify_token(token) is None  # TTL deletion is lazy, so code checks expiry itself
    assert (token, None) not in seeded_dynamo.Table("Tokens").items


def test_patients_are_scoped_through_the_gsi(seeded_dynamo):
    rooms = sorted(p["room"] for p in get_my_patients("N002"))
    assert rooms == ["305", "306", "307", "308"]
    with pytest.raises(PatientAccessDenied):
        get_patient("N002", "309")


def test_downtime_log_keeps_same_second_entries(seeded_dynamo):
    store = downtime_log.default_log_store()
    assert isinstance(store, downtime_log.DynamoLogStore)
    downtime_log.log_event("N002", "P304", "vital", "BP 120/80", store=store)
    downtime_log.log_event("N002", "P304", "med_given", "Insulin 6 units SubQ", store=store)
    assert len(store.entries_for("P304")) == 2  # microsecond ts: no overwrite
    assert len(downtime_log.check_recent_doses("P304", "insulin", store=store)) == 1


def test_alert_ack_store_counts_attempts_and_acks(seeded_dynamo):
    store = alert_app.get_ack_store()
    assert isinstance(store, alert_app.DynamoAckStore)
    store.record_sent("N002", "2024-01-15T02:05:00.000001Z")
    store.record_sent("N002", "2024-01-15T02:10:00.000001Z")
    assert store.latest("N002").attempts == 2
    alert_app.record_ack("N002", store=store)
    assert alert_app.check_ack("N002", store=store) is True
    store.record_sent("N002", "2024-01-15T03:00:00.000001Z")  # a new outage starts over
    assert store.latest("N002").attempts == 1
    assert alert_app.start_new_alert_cycle(store=store) == 3
    assert store.latest("N002") is None


def test_chat_history_returns_recent_turns_oldest_first(seeded_dynamo):
    history = DynamoChatHistory()
    for i in range(4):
        history.append("tok", "nurse", f"msg {i}")
        time.sleep(0.001)
    assert [t["content"] for t in history.recent("tok", 2)] == ["msg 2", "msg 3"]


def test_chat_brief_end_to_end_on_dynamodb_acknowledges_the_alert(seeded_dynamo):
    alert_app.get_ack_store().record_sent("N002", "2024-01-15T02:06:00.000001Z")
    token = auth.create_token("N002")

    response = handle_chat({"token": token, "pin": "1234", "message": ""}, ChatDeps())

    assert [b["room"] for b in response["brief"]] == ["305", "306", "307", "308"]
    assert response["snapshot_ts"] == "2024-01-15T02:05:00Z"
    assert alert_app.check_ack("N002", store=alert_app.get_ack_store()) is True