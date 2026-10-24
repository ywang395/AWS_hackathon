"""Run the routing agent from the command line against the fake or live model.

    python -m backend.agents.routing_agent.cli                      # fake Bedrock
    python -m backend.agents.routing_agent.cli --live --case heparin

``--live`` calls real Bedrock. It needs ``MODEL_ID`` and AWS credentials, which
are read from the environment or from a ``.env`` in ``wardline/`` or the repo
root. Email and event publishing stay in dry-run mode unless ``NOTIFY_DRY_RUN``
or ``DRY_RUN`` is explicitly set to ``0``.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from backend.agents.routing_agent.app import (
    MED_REQUESTED,
    OUTAGE_DETECTED,
    ROUTING_SCAN,
    all_doses,
    handle_routing_event,
    reset_routing_state,
)
from backend.agents.routing_agent.fake_bedrock import (
    CannedBedrockClient,
    insulin_demo_responses,
)
from backend.shared.agent_loop import AgentFeedEvent
from backend.shared.config import load_default_env, load_env_file
from backend.tools import deliveries, escalation
from backend.tools.deliveries import build_request_id

CASES = ("insulin", "heparin", "scan", "outage")


class PrintingAgentFeed:
    """Feed logger that prints each AgentFeed event as one JSON line."""

    def log(self, event: AgentFeedEvent) -> None:
        print(
            json.dumps(
                {
                    "day": event.day,
                    "ts": event.ts,
                    "agent": event.agent,
                    "action": event.action,
                    "detail": event.detail,
                },
                default=str,
                ensure_ascii=True,
                sort_keys=True,
            ),
            flush=True,
        )


def demo_dose(med: str) -> dict[str, Any]:
    """Return the seed-data dose for a demo medication (insulin or heparin)."""

    _, doses = all_doses()
    return next(dose for dose in doses if dose["med"].casefold() == med.casefold())


def demo_event(case: str) -> dict[str, Any]:
    """Build the routing event for a demo case (insulin, heparin, scan, outage)."""

    if case == "scan":
        return {"detail-type": ROUTING_SCAN, "detail": {}}
    if case == "outage":
        snapshot_ts, _ = all_doses()
        return {
            "detail-type": OUTAGE_DETECTED,
            "source": "control",
            "detail": {"started_at": snapshot_ts},
        }
    dose = demo_dose(case)
    return {
        "detail-type": MED_REQUESTED,
        "source": "nurse_agent",
        "detail": {
            "request_id": build_request_id(dose["room"], dose["med"], dose["due_at"]),
            "room": dose["room"],
            "med": dose["med"],
            "requested_by": "N003",
        },
    }


def _load_env(env_file: Path | None) -> None:
    if env_file is not None:
        loaded = load_env_file(env_file)
        source = env_file
    else:
        source, loaded = load_default_env()
    if source is None:
        print("No .env found; using the current environment.", flush=True)
    else:
        print(f"Loaded {len(loaded)} variable(s) from {source}: {', '.join(loaded)}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Wardline routing agent.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Call real Amazon Bedrock (needs MODEL_ID and AWS credentials).",
    )
    parser.add_argument(
        "--case",
        choices=CASES,
        default="insulin",
        help="Demo case to run (fake mode supports insulin only).",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="KEY=VALUE file to load instead of the default wardline/.env or repo-root .env.",
    )
    args = parser.parse_args(argv)

    _load_env(args.env_file)
    # Real email and EventBridge publishing only when explicitly set to 0.
    os.environ.setdefault("NOTIFY_DRY_RUN", "1")
    os.environ.setdefault("DRY_RUN", "1")

    deliveries.reset_deliveries()
    escalation.reset_escalations()
    reset_routing_state()

    if args.live:
        if not os.getenv("MODEL_ID"):
            parser.error("MODEL_ID must be set (shell or .env) for --live runs.")
        model_id: str | None = None
        client: Any | None = None
    else:
        if args.case != "insulin":
            parser.error("Fake mode only scripts the insulin case; add --live.")
        model_id = "local-fake-model"
        client = CannedBedrockClient(
            insulin_demo_responses(demo_dose("insulin")["due_at"])
        )

    result = handle_routing_event(
        demo_event(args.case),
        model_id=model_id,
        bedrock_client=client,
        feed_logger=PrintingAgentFeed(),
    )
    print(
        json.dumps(
            {
                **result,
                "deliveries": deliveries.list_deliveries(),
                "escalations": escalation.list_escalations(),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
