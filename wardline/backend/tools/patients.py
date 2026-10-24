"""
tools/patients.py -- patient-record tools for the nurse agent.

CLAUDE.md section 2 (non-negotiable): "Patient scoping is enforced in
code, never in prompts... No tool accepts nurse_id as a model-supplied
argument."

Concretely that means the functions the *model* gets to call must never
expose a `nurse_id` parameter it could fill in itself. This module
satisfies that with two layers:

  - `get_my_patients(nurse_id, ...)` / `get_patient(nurse_id, room, ...)`
    are plain, fully-testable functions that DO take `nurse_id` -- these
    are what you unit test and what server-side code calls directly once
    `nurse_id` has come back from `shared.auth.authenticate`.
  - `bind_patient_tools(nurse_id, ...)` returns a dict of closures with
    `nurse_id` already baked in from the *verified* token. THIS is what
    gets registered as Bedrock tools for `run_agent()` (CLAUDE.md
    section 8) -- the model can call `get_patient(room="304")` but it can
    never choose whose patients it sees.

Phase 1 reads straight from `data/patients.json`. Phase 2 can swap the
`load_patients` loader for a DynamoDB query against the `assigned_nurse_id`
GSI (via `shared/db.py`) without touching any call site -- every function
below accepts an optional `patients=` list for exactly that reason.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

_DEFAULT_PATIENTS_PATH = Path(__file__).resolve().parents[2] / "data" / "patients.json"


class PatientNotFound(Exception):
    """Raised when a room doesn't exist for ANY nurse."""


class PatientAccessDenied(Exception):
    """
    Raised when a room exists but is not assigned to the requesting nurse.
    Callers (e.g. the nurse agent's reply logic) should turn this into the
    required refusal, never into patient data -- see CLAUDE.md section 13's
    demo case: "Nurse A asks about Nurse B's patient -> refused".
    """


def load_patients(path: Optional[Path] = None) -> list[dict]:
    """
    Load the full patient roster. Default source is `data/patients.json`.
    Pass `path=` to point at a different fixture (tests do this), or
    replace this function entirely in Phase 2 with one that queries
    DynamoDB instead of reading JSON.
    """
    target = path or _DEFAULT_PATIENTS_PATH
    with open(target, "r", encoding="utf-8") as fh:
        return json.load(fh)


def get_my_patients(nurse_id: str, patients: Optional[list[dict]] = None) -> list[dict]:
    """
    Return every patient assigned to `nurse_id`, filtered server-side
    BEFORE anything reaches the model. `nurse_id` must come from
    `shared.auth.verify_token` / `authenticate` -- never from client or
    model input.
    """
    roster = patients if patients is not None else load_patients()
    return [p for p in roster if p.get("assigned_nurse_id") == nurse_id]


def get_patient(nurse_id: str, room: str, patients: Optional[list[dict]] = None) -> dict:
    """
    Return the single patient record for `room`, enforcing ownership:

      - unknown room                   -> PatientNotFound
      - room belongs to another nurse  -> PatientAccessDenied
      - room belongs to `nurse_id`     -> the patient record
    """
    roster = patients if patients is not None else load_patients()
    for p in roster:
        if str(p.get("room")) == str(room):
            if p.get("assigned_nurse_id") != nurse_id:
                raise PatientAccessDenied(f"room {room} is not assigned to nurse {nurse_id}")
            return p
    raise PatientNotFound(f"no patient found in room {room}")


def bind_patient_tools(nurse_id: str, patients: Optional[list[dict]] = None) -> dict[str, Callable]:
    """
    Build the `tool_impls` dict for `run_agent()` (CLAUDE.md section 8),
    with `nurse_id` closed over so the model literally cannot pass a
    different one. The roster is snapshotted once per call (`patients=`)
    so repeated tool calls within a single agent turn see a consistent
    view, matching the "Records as of {snapshot_ts}" requirement.

    Returned callables take only the arguments a model would plausibly
    supply (e.g. `room`) -- never `nurse_id`.
    """
    roster = patients if patients is not None else load_patients()

    def _get_my_patients() -> list[dict]:
        return get_my_patients(nurse_id, patients=roster)

    def _get_patient(room: str) -> dict:
        return get_patient(nurse_id, room, patients=roster)

    return {
        "get_my_patients": _get_my_patients,
        "get_patient": _get_patient,
    }