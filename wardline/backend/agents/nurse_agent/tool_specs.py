"""Amazon Bedrock Converse tool schemas for the nurse agent (CLAUDE.md section 8).

No schema has a nurse_id property (CLAUDE.md section 2): the /chat handler
binds the verified nurse_id in code (nurse_agent/app.py::build_tool_impls).
additionalProperties=false keeps the model to exactly these fields. Tool
names must match the keys build_tool_impls returns; test_app.py checks both.
"""

from __future__ import annotations

from typing import Any

ROOM = {"type": "string", "description": "Patient room number, e.g. 304.", "minLength": 1}
MED = {"type": "string", "description": "Medication name as written in the record.", "minLength": 1}


def _schema(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties or {}, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


def _tool(name: str, description: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {"toolSpec": {"name": name, "description": description, "inputSchema": {"json": schema}}}


NURSE_TOOL_SPECS: list[dict[str, Any]] = [
    _tool("get_my_patients", "List the patients assigned to this nurse.", _schema()),
    _tool(
        "get_patient",
        "Get one patient's chart by room. Refused for rooms not assigned to this nurse.",
        _schema({"room": ROOM}, ["room"]),
    ),
    _tool(
        "check_recent_doses",
        "List doses of a medication logged for a patient in the last 4 hours, with who gave them and when.",
        _schema({"room": ROOM, "med": MED}, ["room", "med"]),
    ),
    _tool(
        "log_event",
        "Record a downtime event. A med_given that repeats a recent dose is blocked and "
        "returns status 'stop' with who gave it and when.",
        _schema(
            {
                "room": ROOM,
                "event_type": {"type": "string", "enum": ["med_given", "vital", "note"]},
                "detail": {
                    "type": "string",
                    "description": "What happened, e.g. 'Insulin 6 units SubQ'.",
                    "minLength": 1,
                },
                "double_checked_by": {
                    "type": "string",
                    "description": "Second nurse who verified the dose, if any.",
                },
            },
            ["room", "event_type", "detail"],
        ),
    ),
    _tool(
        "request_med",
        "Ask the routing agent to deliver an ordered medication that is unavailable on the unit.",
        _schema({"room": ROOM, "med": MED}, ["room", "med"]),
    ),
    _tool(
        "get_delivery_status",
        "List medication deliveries heading to a patient's room.",
        _schema({"room": ROOM}, ["room"]),
    ),
]

NURSE_TOOL_NAMES = frozenset(spec["toolSpec"]["name"] for spec in NURSE_TOOL_SPECS)