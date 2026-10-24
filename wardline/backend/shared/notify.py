"""
shared/notify.py -- Amazon SES v2 email helper for the alert agent (nurse
alerts and resends) and, once wired, the routing agent (runner dispatch).

Replaces shared/sms.py: CLAUDE.md section 3 moved notifications to email
because the workshop account has no SMS origination.

Environment (CLAUDE.md sections 3 and 11, template.yaml Parameters):
    NOTIFY_DRY_RUN      "1" (default) logs instead of sending; "0" sends for real
    FROM_EMAIL          SES-verified sender. Required when NOTIFY_DRY_RUN=0
    NOTIFY_TO_OVERRIDE  optional. Redirects every live email to one
                        SES-verified inbox. Seed data uses synthetic
                        @example.com addresses (CLAUDE.md section 2), which the
                        SES sandbox cannot deliver to, so set this to test real
                        delivery without putting real addresses in data/.
    AWS_REGION          SES region (default us-east-1)

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

DEFAULT_REGION = "us-east-1"


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


def send_email(to: str, subject: str, body: str, *, client: Any | None = None) -> NotifyResult:
    """Send one plain-text email through SES v2, or log it when dry-run is on.

    Never raises on a delivery failure: one bad address must not abort an
    alert sweep over several nurses, so callers check `result.status`.
    `client` takes an injected sesv2 client (tests); otherwise one is built.
    """
    if dry_run_enabled():
        result = NotifyResult(to=to, subject=subject, status="dry_run", delivered_to=to)
        _log(result, body=body)
        return result

    sender = os.environ.get("FROM_EMAIL", "").strip()
    recipient = os.environ.get("NOTIFY_TO_OVERRIDE", "").strip() or to
    if not sender:
        result = NotifyResult(
            to=to, subject=subject, status="error", delivered_to=recipient,
            error="FROM_EMAIL is not configured.",
        )
        _log(result)
        return result

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
        result = NotifyResult(
            to=to, subject=subject, status="sent", delivered_to=recipient,
            message_id=response.get("MessageId"),
        )
    except Exception as exc:  # noqa: BLE001 - reported through the result, never raised
        result = NotifyResult(
            to=to, subject=subject, status="error", delivered_to=recipient, error=str(exc),
        )
    _log(result)
    return result


def send_bulk_email(recipients: list[str], subject: str, body: str) -> list[NotifyResult]:
    """Same message to many addresses. Not atomic: one failure does not stop the rest."""
    return [send_email(to, subject, body) for to in recipients]


def _ses_client() -> Any:
    import boto3  # lazy: dry-run and unit tests need no boto3

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or DEFAULT_REGION
    return boto3.client("sesv2", region_name=region)


def _log(result: NotifyResult, *, body: str | None = None) -> None:
    record: dict[str, Any] = {"event": "email", **asdict(result)}
    if body is not None:
        record["body"] = body
    print(json.dumps(record))