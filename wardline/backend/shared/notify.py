"""
shared/notify.py -- Amazon SES v2 email helper shared by the alert agent
(nurse alerts and resends) and the routing agent (runner dispatch).

Replaces shared/sms.py: CLAUDE.md section 3 moved notifications to email
because the workshop account has no SMS origination.

One contract for both agents: send_email() returns a NotifyResult and never
raises. The alert sweep relies on that, so one bad address cannot stop the
other nurses' alerts. The routing tool turns status "error" into a
RoutingToolError itself, so the model knows the runner was not reached.

Environment (CLAUDE.md sections 3 and 11, template.yaml Parameters):
    NOTIFY_DRY_RUN      "1" (default) logs instead of sending; "0" sends for real
    FROM_EMAIL          SES-verified sender. Required when NOTIFY_DRY_RUN=0
    NOTIFY_TO_OVERRIDE  optional. Redirects every live email to one
                        SES-verified inbox. Seed data uses synthetic
                        @example.com addresses (CLAUDE.md section 2), which the
                        SES sandbox cannot deliver to.
    SES_REGION          optional SES region; otherwise AWS_REGION, then us-east-1
                        (shared/config.resolve_region)

Every message is validated, dry runs included: each address must be one
bare mailbox and the subject one line, so text a model passes through a tool
cannot add recipients or headers.

Live sends never log the message body: alert emails carry a secure-link
token (shared/auth.py), and CloudWatch logs are not the place for it. Dry
runs print the body so a developer can open the link locally.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from backend.shared.config import resolve_region

MAX_SUBJECT_LENGTH = 200


def dry_run_enabled() -> bool:
    """Dry run unless NOTIFY_DRY_RUN is explicitly off, so a missing env var never sends mail."""
    return os.environ.get("NOTIFY_DRY_RUN", "1").strip().lower() not in {"0", "false", "no", "off"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class NotifyResult:
    to: str
    subject: str
    status: str  # "dry_run" | "sent" | "error"
    delivered_to: str | None = None  # differs from `to` when NOTIFY_TO_OVERRIDE is set
    message_id: str | None = None
    error: str | None = None
    sent_at: str = field(default_factory=_now_iso)


def _address_problem(value: Any, name: str) -> str | None:
    """None when `value` is a single bare mailbox, otherwise what is wrong with it."""
    if not isinstance(value, str) or not value.strip():
        return f"{name} must be a non-empty email address."
    local, sep, domain = value.strip().partition("@")
    if not sep or not local or "." not in domain or any(c in value for c in " \r\n,;<>"):
        return f"{name} must be a single email address."
    return None


def _message_problem(to: Any, subject: Any, body: Any) -> str | None:
    problem = _address_problem(to, "to")
    if problem:
        return problem
    if not isinstance(subject, str) or not subject.strip():
        return "subject must be a non-empty string."
    if "\r" in subject or "\n" in subject:
        return "subject must be a single line."
    if len(subject) > MAX_SUBJECT_LENGTH:
        return f"subject must be {MAX_SUBJECT_LENGTH} characters or fewer."
    if not isinstance(body, str) or not body.strip():
        return "body must be a non-empty string."
    return None


def send_email(to: str, subject: str, body: str, *, client: Any | None = None) -> NotifyResult:
    """Send one plain-text email through SES v2, or log it when dry-run is on.

    Never raises: invalid input, missing configuration and SES failures all
    come back as status "error" with the reason in `error`. `client` takes
    an injected sesv2 client (tests); otherwise one is built.
    """
    problem = _message_problem(to, subject, body)
    if problem:
        return _failed(str(to), str(subject), problem)
    to, subject = to.strip(), subject.strip()

    if dry_run_enabled():
        result = NotifyResult(to=to, subject=subject, status="dry_run", delivered_to=to)
        _log(result, body=body)
        return result

    sender = os.environ.get("FROM_EMAIL", "").strip()
    recipient = os.environ.get("NOTIFY_TO_OVERRIDE", "").strip() or to
    if not sender:
        return _failed(to, subject, "FROM_EMAIL is not configured.", delivered_to=recipient)
    problem = _address_problem(sender, "FROM_EMAIL") or _address_problem(recipient, "NOTIFY_TO_OVERRIDE")
    if problem:
        return _failed(to, subject, problem, delivered_to=recipient)

    try:
        ses = client if client is not None else _ses_client()
        response = ses.send_email(
            FromEmailAddress=sender,
            Destination={"ToAddresses": [recipient]},
            Content={
                "Simple": {
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
                }
            },
        )
        message_id = response.get("MessageId") if isinstance(response, dict) else None
        if not message_id:
            return _failed(to, subject, "SES response is missing MessageId.", delivered_to=recipient)
        result = NotifyResult(to=to, subject=subject, status="sent", delivered_to=recipient, message_id=message_id)
    except Exception as exc:  # noqa: BLE001 - reported through the result, never raised
        return _failed(to, subject, str(exc), delivered_to=recipient)
    _log(result)
    return result


def send_bulk_email(recipients: list[str], subject: str, body: str) -> list[NotifyResult]:
    """Same message to many addresses. Not atomic: one failure does not stop the rest."""
    return [send_email(to, subject, body) for to in recipients]


def _failed(to: str, subject: str, error: str, *, delivered_to: str | None = None) -> NotifyResult:
    result = NotifyResult(to=to, subject=subject, status="error", delivered_to=delivered_to, error=error)
    _log(result)
    return result


def _ses_client() -> Any:
    import boto3  # lazy: dry-run and unit tests need no boto3

    return boto3.client("sesv2", region_name=resolve_region(None, "SES_REGION"))


def _log(result: NotifyResult, *, body: str | None = None) -> None:
    record: dict[str, Any] = {"event": "email", **asdict(result)}
    if body is not None:
        record["body"] = body
    print(json.dumps(record), flush=True)