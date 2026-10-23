"""
Unit tests for `tools/patients.py`.

Uses a small, self-contained fixture roster (NOT the real
`data/patients.json`) so these tests stay stable even as the seed data
evolves, and so the ownership-check behaviour required by CLAUDE.md
section 2 is pinned down independently of any particular demo dataset.
"""

import pytest

from backend.tools.patients import (
    PatientAccessDenied,
    PatientNotFound,
    bind_patient_tools,
    get_my_patients,
    get_patient,
)

FIXTURE_PATIENTS = [
    {"patient_id": "P1", "assigned_nurse_id": "N002", "room": "101", "name": "Fixture One"},
    {"patient_id": "P2", "assigned_nurse_id": "N002", "room": "102", "name": "Fixture Two"},
    {"patient_id": "P3", "assigned_nurse_id": "N003", "room": "201", "name": "Fixture Three"},
]


def test_get_my_patients_returns_only_assigned_patients():
    result = get_my_patients("N002", patients=FIXTURE_PATIENTS)
    rooms = {p["room"] for p in result}
    assert rooms == {"101", "102"}


def test_get_my_patients_returns_empty_list_for_nurse_with_no_patients():
    assert get_my_patients("N999", patients=FIXTURE_PATIENTS) == []


def test_get_patient_returns_record_for_owning_nurse():
    patient = get_patient("N002", "101", patients=FIXTURE_PATIENTS)
    assert patient["patient_id"] == "P1"


def test_get_patient_denies_access_to_another_nurses_patient():
    # The non-negotiable refusal demo (CLAUDE.md section 13): "Nurse A asks
    # about Nurse B's patient -> refused".
    with pytest.raises(PatientAccessDenied):
        get_patient("N002", "201", patients=FIXTURE_PATIENTS)


def test_get_patient_raises_not_found_for_unknown_room():
    with pytest.raises(PatientNotFound):
        get_patient("N002", "999", patients=FIXTURE_PATIENTS)


def test_bind_patient_tools_closes_over_nurse_id():
    tools = bind_patient_tools("N002", patients=FIXTURE_PATIENTS)

    assert set(tools.keys()) == {"get_my_patients", "get_patient"}
    rooms = {p["room"] for p in tools["get_my_patients"]()}
    assert rooms == {"101", "102"}
    assert tools["get_patient"]("101")["patient_id"] == "P1"


def test_bound_get_patient_still_denies_other_nurses_rooms():
    # The bound tool takes only `room` -- there is no way for a model to
    # pass a different nurse_id and see P3.
    tools = bind_patient_tools("N002", patients=FIXTURE_PATIENTS)
    with pytest.raises(PatientAccessDenied):
        tools["get_patient"]("201")