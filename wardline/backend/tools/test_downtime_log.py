"""
Unit tests for `tools/downtime_log.py`, including the double-dose
detection flow described in the nurse agent's system prompt (CLAUDE.md
section 12): "Before confirming a med_given log, call check_recent_doses.
If already given, say STOP and who/when."
"""

from backend.tools.downtime_log import (
    InMemoryLogStore,
    bind_downtime_log_tools,
    check_recent_doses,
    log_event,
)


def test_log_event_appends_an_entry_with_expected_fields():
    store = InMemoryLogStore()
    entry = log_event("N002", "P304", "med_given", "Insulin 6 units SubQ", store=store)

    assert entry.nurse_id == "N002"
    assert entry.patient_id == "P304"
    assert entry.type == "med_given"
    assert "Insulin" in entry.detail
    assert store.entries_for("P304") == [entry]


def test_check_recent_doses_is_empty_when_nothing_logged_yet():
    store = InMemoryLogStore()
    assert check_recent_doses("P304", "Insulin", store=store) == []


def test_check_recent_doses_detects_a_dose_already_given():
    store = InMemoryLogStore()
    log_event(
        "N002", "P304", "med_given", "Insulin 6 units SubQ",
        store=store, now="2024-01-15T02:00:00Z",
    )

    matches = check_recent_doses("P304", "Insulin", store=store, now="2024-01-15T02:30:00Z")

    assert len(matches) == 1
    assert matches[0].nurse_id == "N002"
    assert matches[0].ts == "2024-01-15T02:00:00Z"


def test_check_recent_doses_ignores_doses_outside_the_window():
    store = InMemoryLogStore()
    log_event(
        "N002", "P304", "med_given", "Insulin 6 units SubQ",
        store=store, now="2024-01-15T02:00:00Z",
    )

    # 5 hours later, past the default 240-minute window -- safe to give again.
    matches = check_recent_doses("P304", "Insulin", store=store, now="2024-01-15T07:00:00Z")

    assert matches == []


def test_check_recent_doses_ignores_other_medications():
    store = InMemoryLogStore()
    log_event(
        "N002", "P304", "med_given", "Insulin 6 units SubQ",
        store=store, now="2024-01-15T02:00:00Z",
    )

    matches = check_recent_doses("P304", "Heparin", store=store, now="2024-01-15T02:05:00Z")

    assert matches == []


def test_check_recent_doses_ignores_non_med_given_entries():
    store = InMemoryLogStore()
    log_event(
        "N002", "P304", "note", "Patient mentioned Insulin pen is missing",
        store=store, now="2024-01-15T02:00:00Z",
    )

    matches = check_recent_doses("P304", "Insulin", store=store, now="2024-01-15T02:05:00Z")

    assert matches == []


def test_bind_downtime_log_tools_full_double_dose_flow():
    """
    End-to-end simulation of the demo case: a nurse tries to log a dose
    that was already given -- `check_recent_doses` must surface who gave
    it and when, matching what the system prompt tells the model to
    report back as "STOP".
    """
    tools = bind_downtime_log_tools("N002", "P304")

    first = tools["check_recent_doses"]("Insulin")
    assert first == []  # nothing logged yet -- safe to proceed

    tools["log_event"]("med_given", "Insulin 6 units SubQ")

    second = tools["check_recent_doses"]("Insulin")
    assert len(second) == 1
    assert second[0]["nurse_id"] == "N002"
    assert "Insulin" in second[0]["detail"]