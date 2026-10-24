"""Amazon Bedrock Converse tool schemas for the routing agent."""

from __future__ import annotations

from typing import Any


def _object_schema(
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


ROUTING_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "toolSpec": {
            "name": "get_doses_due",
            "description": (
                "Return snapshot_ts and doses from the patient records with "
                "room, medication, due time, criticality, home cabinet, "
                "needs_routing, existing_delivery, and open_escalation_id. "
                "Pass room and med to resolve one requested dose; omit both "
                "to scan doses due within 60 minutes of the snapshot."
            ),
            "inputSchema": {
                "json": _object_schema(
                    {
                        "room": {
                            "type": "string",
                            "description": "Patient room from the event.",
                            "minLength": 1,
                        },
                        "med": {
                            "type": "string",
                            "description": "Medication name from the event.",
                            "minLength": 1,
                        },
                    }
                )
            },
        }
    },
    {
        "toolSpec": {
            "name": "find_stock",
            "description": (
                "Find positive-stock locations for a medication. Results are "
                "ordered nearest first."
            ),
            "inputSchema": {
                "json": _object_schema(
                    {
                        "med": {
                            "type": "string",
                            "description": "Exact medication name from the record.",
                            "minLength": 1,
                        }
                    },
                    ["med"],
                )
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_runners",
            "description": (
                "List available medication runners with their location and "
                "current workload."
            ),
            "inputSchema": {"json": _object_schema()},
        }
    },
    {
        "toolSpec": {
            "name": "create_delivery",
            "description": (
                "Create an idempotent delivery after selecting stock and a "
                "runner. Duplicate room, medication, and due time returns the "
                "existing delivery."
            ),
            "inputSchema": {
                "json": _object_schema(
                    {
                        "room": {
                            "type": "string",
                            "description": "Destination patient room.",
                            "minLength": 1,
                        },
                        "med": {
                            "type": "string",
                            "description": "Medication name from the care record.",
                            "minLength": 1,
                        },
                        "due_at": {
                            "type": "string",
                            "description": "ISO 8601 due time with timezone.",
                            "minLength": 1,
                        },
                        "from_location": {
                            "type": "string",
                            "description": "Stock location selected from find_stock.",
                            "minLength": 1,
                        },
                        "priority": {
                            "type": "string",
                            "description": "Routing priority such as critical or urgent.",
                            "minLength": 1,
                        },
                        "runner_id": {
                            "type": "string",
                            "description": "Runner selected from get_runners.",
                            "minLength": 1,
                        },
                    },
                    [
                        "room",
                        "med",
                        "due_at",
                        "from_location",
                        "priority",
                        "runner_id",
                    ],
                )
            },
        }
    },
    {
        "toolSpec": {
            "name": "send_email",
            "description": (
                "Notify a selected runner by email through the server-configured "
                "SES sender. The runner's email address is resolved in code. "
                "Returns status 'sent' or 'dry_run'."
            ),
            "inputSchema": {
                "json": _object_schema(
                    {
                        "runner_id": {
                            "type": "string",
                            "description": "Runner selected from get_runners.",
                            "minLength": 1,
                        },
                        "subject": {
                            "type": "string",
                            "description": "Brief one-line email subject.",
                            "minLength": 1,
                            "maxLength": 200,
                        },
                        "body": {
                            "type": "string",
                            "description": (
                                "Plain-text email body: delivery room, "
                                "medication, source, priority, and due time."
                            ),
                            "minLength": 1,
                        },
                    },
                    ["runner_id", "subject", "body"],
                )
            },
        }
    },
    {
        "toolSpec": {
            "name": "escalate",
            "description": (
                "Create a human escalation when safe routing cannot be proven "
                "and publish EscalationNeeded so the charge nurse is alerted. "
                "The routing agent identity is bound by server code."
            ),
            "inputSchema": {
                "json": _object_schema(
                    {
                        "reason": {
                            "type": "string",
                            "description": "Specific factual reason for escalation.",
                            "minLength": 1,
                        },
                        "room": {
                            "type": "string",
                            "description": "Affected patient room.",
                            "minLength": 1,
                        },
                        "to_role": {
                            "type": "string",
                            "description": "Role receiving the escalation.",
                            "enum": ["charge"],
                        },
                        "med": {
                            "type": "string",
                            "description": (
                                "Affected medication; prevents duplicate "
                                "escalations for the same dose."
                            ),
                            "minLength": 1,
                        },
                    },
                    ["reason", "room", "to_role"],
                )
            },
        }
    },
]

ROUTING_TOOL_NAMES = frozenset(
    spec["toolSpec"]["name"] for spec in ROUTING_TOOL_SPECS
)
