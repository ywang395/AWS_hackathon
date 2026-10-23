"""
backend/agents/alert_agent/app.py -- the Alert agent.

CLAUDE.md section 8:
    Trigger: OutageDetected, AckCheck, EscalationNeeded
    Goal:    every on-shift nurse reached + acked within 5 min
    Tools:   get_on_shift_staff, get_nurse_workload, send_sms, check_ack,
             schedule_ack_check, escalate

This module implements the deterministic parts of that loop directly
(message building, ack tracking, resend, escalation) so the whole thing is
testable TODAY with `SMS_DRY_RUN=1` and no dependency on Bedrock or AWS.

It is written so the SAME functions double as the `tool_impls` for
`shared.agent_loop.run_agent()` once that module exists (see
`get_tool_impls()` near the bottom) -- Sunny's Bedrock loop can be wired
in without this file changing, matching CLAUDE.md section 8's "Tools
bound with server-side context" pattern.

Lambda entry point: `handler(event, context)`, dispatched on the
EventBridge `detail-type` (CLAUDE.md section 6).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol

from backend.shared import auth
from backend.shared.sms import SmsResult, send_sms

# tools/escalation.py is owned by the routing-agent workstream (CLAUDE.md
# section 9) and may not be implemented yet at any given time. Fall back
# to a minimal local escalation writer so this module is fully usable on
# its own; once the real `tools.escalation.escalate` lands, it is picked
# up automatically on next import and nothing here needs to change.
try:
    from backend.tools.escalation import escalate as _tools_escalate  # type: ignore
except ImportError:  # pragma: no cover - exercised once tools/escalation.py lands
    _tools_escalate = None


DEFAULT_ACK_WINDOW_MINUTES = 5  # CLAUDE.md section 8 goal: acked within 5 min
DEFAULT_MAX_ATTEMPTS = 3  # initial send + 2 resends before escalating
FRONTEND_BASE_URL_ENV = "FRONTEND_BASE_URL"
_DEFAULT_FRONTEND_BASE_URL = "https://wardline.example/nurse"


# ---------------------------------------------------------------------------
# Ack tracking (mirrors the `Alerts` table: PK nurse_id, SK sent_at)
# ---------------------------------------------------------------------------

@dataclass
class AlertAttempt:
    nurse_id: str
    sent_at: str
    attempts: int
    acked: bool = False
    acked_at: Optional[str] = None


class AckStore(Protocol):
    def record_sent(self, nurse_id: str, sent_at: str) -> AlertAttempt: ...

    def record_ack(self, nurse_id: str, acked_at: str) -> None: ...

    def latest(self, nurse_id: str) -> Optional[AlertAttempt]: ...


class InMemoryAckStore:
    """
    Default `AckStore`. Suitable for Phase 1 / unit tests; swap for a
    DynamoDB-backed store over the `Alerts` table (`shared/db.py`) in
    Phase 2 -- every function below takes `store=` for exactly that swap.
    """

    def __init__(self) -> None:
        self._latest: dict[str, AlertAttempt] = {}

    def record_sent(self, nurse_id: str, sent_at: str) -> AlertAttempt:
        existing = self._latest.get(nurse_id)
        attempts = (existing.attempts + 1) if existing and not existing.acked else 1
        attempt = AlertAttempt(nurse_id=nurse_id, sent_at=sent_at, attempts=attempts)
        self._latest[nurse_id] = attempt
        return attempt

    def record_ack(self, nurse_id: str, acked_at: str) -> None:
        existing = self._latest.get(nurse_id)
        if existing:
            existing.acked = True
            existing.acked_at = acked_at

    def latest(self, nurse_id: str) -> Optional[AlertAttempt]:
        return self._latest.get(nurse_id)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Staff lookup (tools: get_on_shift_staff, get_nurse_workload)
# ---------------------------------------------------------------------------

_DEFAULT_NURSES_PATH = Path(__file__).resolve().parents[3] / "data" / "nurses.json"


def _load_nurses(path: Optional[Path] = None) -> list[dict]:
    target = path or _DEFAULT_NURSES_PATH
    with open(target, "r", encoding="utf-8") as fh:
        return json.load(fh)


def get_on_shift_staff(
    roles: tuple[str, ...] = ("nurse", "charge"),
    nurses: Optional[list[dict]] = None,
) -> list[dict]:
    """
    Everyone the alert agent is responsible for reaching: on-shift staff
    in `roles` (default: bedside nurses + the charge nurse; runners are
    the routing agent's concern and are excluded by default).
    """
    roster = nurses if nurses is not None else _load_nurses()
    return [n for n in roster if n.get("on_shift") and n.get("role") in roles]


def get_nurse_workload(nurse_id: str, patients: Optional[list[dict]] = None) -> int:
    """
    Number of patients currently assigned to `nurse_id` -- lets the agent
    prioritize (e.g. heavier assignments first) when ordering sends.
    Imported lazily from `tools.patients` so callers that do not need this
    helper do not take on a hard dependency at module-import time.
    """
    from backend.tools.patients import get_my_patients  # local import: optional dependency

    return len(get_my_patients(nurse_id, patients=patients))


# ---------------------------------------------------------------------------
# Message builder
# ---------------------------------------------------------------------------

def build_secure_link(nurse_id: str, *, base_url: Optional[str] = None, token_store=None) -> str:
    """
    Mint a fresh `shared.auth` token for `nurse_id` and build the link the
    SMS points at. A new token is minted per send/resend so an
    intercepted text message cannot be replayed indefinitely.
    """
    token = auth.create_token(nurse_id, store=token_store)
    root = base_url or os.environ.get(FRONTEND_BASE_URL_ENV, _DEFAULT_FRONTEND_BASE_URL)
    return f"{root}?token={token}"


def build_outage_message(nurse: dict, *, base_url: Optional[str] = None, token_store=None) -> str:
    """First-contact alert: systems are down, come acknowledge via the secure link."""
    link = build_secure_link(nurse["nurse_id"], base_url=base_url, token_store=token_store)
    name = nurse.get("name", "there")
    return f"Wardline: Hospital systems are down. Hi {name}, please confirm you're online and review your patients now: {link}"


def build_resend_message(
    nurse: dict,
    attempt: int,
    *,
    base_url: Optional[str] = None,
    token_store=None,
) -> str:
    """Reminder text for an unacknowledged alert (attempt 2+)."""
    link = build_secure_link(nurse["nurse_id"], base_url=base_url, token_store=token_store)
    name = nurse.get("name", "there")
    return f"Wardline reminder ({attempt}): {name}, please confirm receipt of the downtime alert now: {link}"


# ---------------------------------------------------------------------------
# Escalation (tool: escalate)
# ---------------------------------------------------------------------------

_fallback_escalations: list[dict] = []


def escalate(
    reason: str,
    *,
    nurse_id: Optional[str] = None,
    room: Optional[str] = None,
    to_role: str = "charge",
    source_agent: str = "alert_agent",
    escalations: Optional[list[dict]] = None,
) -> dict:
    """
    Raise a human escalation (CLAUDE.md section 2: the AI escalates
    instead of guessing or acting on its own). Delegates to
    `tools.escalation.escalate` once that module exists (Sunny's
    workstream, CLAUDE.md section 9); until then, appends to the
    `escalations` list passed in (or a module-level fallback list) so the
    alert agent is fully usable on its own.
    """
    if _tools_escalate is not None:
        return _tools_escalate(
            reason=reason, nurse_id=nurse_id, room=room, to_role=to_role, source_agent=source_agent
        )

    record = {
        "reason": reason,
        "nurse_id": nurse_id,
        "room": room,
        "to_role": to_role,
        "source_agent": source_agent,
        "created_at": _now_iso(),
        "resolved": False,
    }
    target = escalations if escalations is not None else _fallback_escalations
    target.append(record)
    return record


# ---------------------------------------------------------------------------
# Ack tracking tools: send/resend + check_ack + schedule_ack_check
# ---------------------------------------------------------------------------

def send_alert(
    nurse: dict,
    *,
    store: AckStore,
    attempt: int = 1,
    base_url: Optional[str] = None,
    token_store=None,
) -> SmsResult:
    """
    Send (or resend, if `attempt > 1`) the outage alert to one nurse and
    record the attempt in `store`. Uses `shared.sms.send_sms`, so it
    automatically honors `SMS_DRY_RUN`.
    """
    message = (
        build_outage_message(nurse, base_url=base_url, token_store=token_store)
        if attempt == 1
        else build_resend_message(nurse, attempt, base_url=base_url, token_store=token_store)
    )
    result = send_sms(nurse["phone"], message)
    store.record_sent(nurse["nurse_id"], _now_iso())
    return result


def check_ack(nurse_id: str, *, store: AckStore) -> bool:
    """Tool: has this nurse acknowledged their most recent alert?"""
    attempt = store.latest(nurse_id)
    return bool(attempt and attempt.acked)


def record_ack(nurse_id: str, *, store: AckStore, acked_at: Optional[str] = None) -> None:
    """
    Called when a nurse's acknowledgment actually arrives (e.g. opening
    their secure link counts as an ack). Not itself one of CLAUDE.md
    section 8's named tools, but `check_ack` is meaningless without a way
    to set it -- kept here so the API layer / `tools/escalation.py` has
    one obvious place to call into once the link-open handler exists.
    """
    store.record_ack(nurse_id, acked_at or _now_iso())


def schedule_ack_check(nurse_id: str, *, delay_minutes: int = DEFAULT_ACK_WINDOW_MINUTES) -> dict:
    """
    Tool: ask EventBridge Scheduler (CLAUDE.md section 6 `AckCheck`
    event, owned by Lin's `template.yaml`) to re-invoke this agent for
    `nurse_id` in `delay_minutes`. Phase 1 stub: returns the schedule
    request as data instead of calling AWS, so it is testable without a
    live Scheduler. Phase 2: replace the body with a `boto3 scheduler` /
    EventBridge `put_events` call behind this same signature.
    """
    run_at = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
    return {"nurse_id": nurse_id, "run_at": run_at.strftime("%Y-%m-%dT%H:%M:%SZ")}


# ---------------------------------------------------------------------------
# The ack sweep -- "every on-shift nurse reached + acked within 5 min"
# ---------------------------------------------------------------------------

def run_ack_sweep(
    *,
    store: AckStore,
    nurses: Optional[list[dict]] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_url: Optional[str] = None,
    token_store=None,
    escalations: Optional[list[dict]] = None,
) -> list[dict]:
    """
    Core alert-agent cycle, run on `OutageDetected` and again on every
    `AckCheck`:

      1. For every on-shift nurse/charge who has not acked yet: send
         (first contact) or resend (follow-up), incrementing their
         attempt count.
      2. Once a nurse has had `max_attempts` sends with no ack, escalate
         to the charge nurse instead of sending again -- this is the
         "Kim doesn't ack -> resend -> escalation to Dana" demo case
         (CLAUDE.md section 13).

    Returns one summary dict per on-shift nurse describing what happened,
    useful both for the demo's live feed and for test assertions.
    """
    staff = get_on_shift_staff(nurses=nurses)
    outcomes = []

    for nurse in staff:
        nurse_id = nurse["nurse_id"]
        if check_ack(nurse_id, store=store):
            outcomes.append({"nurse_id": nurse_id, "action": "none", "acked": True})
            continue

        prior = store.latest(nurse_id)
        next_attempt = (prior.attempts + 1) if prior else 1

        if next_attempt > max_attempts:
            escalation = escalate(
                reason=f"No ack from {nurse.get('name', nurse_id)} after {max_attempts} attempts",
                nurse_id=nurse_id,
                to_role="charge",
                escalations=escalations,
            )
            outcomes.append({"nurse_id": nurse_id, "action": "escalated", "escalation": escalation})
            continue

        result = send_alert(nurse, store=store, attempt=next_attempt, base_url=base_url, token_store=token_store)
        outcomes.append(
            {
                "nurse_id": nurse_id,
                "action": "sent" if next_attempt == 1 else "resent",
                "attempt": next_attempt,
                "sms_status": result.status,
            }
        )

    return outcomes


# ---------------------------------------------------------------------------
# Bedrock-tool bindings + Lambda handler
# ---------------------------------------------------------------------------

def get_tool_impls(*, store: Optional[AckStore] = None) -> dict[str, Callable]:
    """
    `tool_impls` dict shaped for `shared.agent_loop.run_agent()` (CLAUDE.md
    section 8). Not wired into Bedrock yet (`agent_loop.py` is still a
    stub) -- this exists so that integration is a one-line change once it
    is ready, with no rework of the logic above.
    """
    active_store = store or InMemoryAckStore()
    return {
        "get_on_shift_staff": get_on_shift_staff,
        "get_nurse_workload": get_nurse_workload,
        "send_sms": send_sms,
        "check_ack": lambda nurse_id: check_ack(nurse_id, store=active_store),
        "schedule_ack_check": schedule_ack_check,
        "escalate": escalate,
    }


_handler_store_singleton: Optional[InMemoryAckStore] = None


def _handler_store() -> InMemoryAckStore:
    """
    Lazily-created store reused across invocations within the same warm
    Lambda container. Phase 1 placeholder only -- Phase 2 must replace
    this with a DynamoDB-backed `AckStore` so state survives cold starts
    and is shared across concurrent containers.
    """
    global _handler_store_singleton
    if _handler_store_singleton is None:
        _handler_store_singleton = InMemoryAckStore()
    return _handler_store_singleton


def handler(event: dict, context=None) -> dict:
    """
    Lambda entry point. Dispatches on the EventBridge `detail-type`
    (CLAUDE.md section 6): `OutageDetected` and `AckCheck` both run the
    ack sweep; `EscalationNeeded` raises an escalation routed to this
    agent (e.g. forwarded from the routing agent, per section 8's trigger
    list).
    """
    detail_type = event.get("detail-type")
    detail = event.get("detail", {})
    store = _handler_store()

    if detail_type in ("OutageDetected", "AckCheck"):
        return {"outcomes": run_ack_sweep(store=store)}

    if detail_type == "EscalationNeeded":
        return {
            "escalation": escalate(
                reason=detail.get("reason", "escalation requested"),
                room=detail.get("room"),
                to_role=detail.get("to_role", "charge"),
                source_agent=detail.get("source_agent", "unknown"),
            )
        }

    return {"error": f"unhandled detail-type: {detail_type}"}