"""
shared/sms.py -- Amazon SNS SMS helper shared by the alert and routing
agents.

CLAUDE.md section 11: `SMS_DRY_RUN=1` env var -> log instead of send
(saves SNS sandbox quota during development). CLAUDE.md section 9: Lin
owns verifying demo phone numbers in the SNS sandbox; this module is what
actually calls SNS once that verification is done.

`boto3` is imported lazily, inside `send_sms`, and ONLY on the
non-dry-run path. That means this module -- and anything that imports it,
like `agents/alert_agent/app.py` -- can be unit tested with
`SMS_DRY_RUN=1` in an environment that has no boto3 package and no AWS
credentials configured at all.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional


def _dry_run_enabled() -> bool:
    return os.environ.get("SMS_DRY_RUN", "0") == "1"


@dataclass
class SmsResult:
    to: str
    body: str
    status: str  # "dry_run" | "sent" | "error"
    message_id: Optional[str] = None
    error: Optional[str] = None
    sent_at: str = ""

    def __post_init__(self) -> None:
        if not self.sent_at:
            self.sent_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def send_sms(to: str, body: str) -> SmsResult:
    """
    Send one SMS via Amazon SNS, or log it instead when `SMS_DRY_RUN=1`.

    Every call also emits a single-line JSON log record (CLAUDE.md
    section 11: "Logging: JSON to CloudWatch"), which doubles as the
    record an agent's tool-call wrapper can forward into `AgentFeed`.

    Returns an `SmsResult` rather than raising on delivery failure, so one
    bad phone number does not take down an entire alert/resend sweep over
    several nurses -- callers should check `result.status`.
    """
    if _dry_run_enabled():
        result = SmsResult(to=to, body=body, status="dry_run", message_id=None)
        _log(result)
        return result

    try:
        import boto3  # local import: keeps boto3 optional for dry-run-only test runs

        client = boto3.client("sns")
        response = client.publish(PhoneNumber=to, Message=body)
        result = SmsResult(to=to, body=body, status="sent", message_id=response.get("MessageId"))
    except Exception as exc:  # noqa: BLE001 - surfaced via SmsResult, never re-raised
        result = SmsResult(to=to, body=body, status="error", error=str(exc))

    _log(result)
    return result


def send_bulk_sms(recipients: list[str], body: str) -> list[SmsResult]:
    """
    Convenience wrapper for the alert agent (texts every on-shift nurse)
    and the routing agent (texts a runner) -- same message, many numbers.
    Not atomic: a failure for one recipient does not stop the others.
    """
    return [send_sms(to, body) for to in recipients]


def _log(result: SmsResult) -> None:
    print(json.dumps({"event": "sms", **asdict(result)}))