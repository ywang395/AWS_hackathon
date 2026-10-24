"""Amazon SES v2 email helper shared by Wardline agents (replaces shared/sms.py).

``NOTIFY_DRY_RUN`` defaults to on: unless it is explicitly set to a false
value, messages are printed as JSON and no AWS client is created. In the SES
sandbox both ``FROM_EMAIL`` and every recipient must be verified identities.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from backend.shared.config import resolve_region

MAX_SUBJECT_LENGTH = 200

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class NotifyError(RuntimeError):
    """Base exception for notification failures."""


class NotifyConfigurationError(NotifyError):
    """Raised when notification configuration is missing or invalid."""


class NotifyValidationError(NotifyError):
    """Raised when a message does not satisfy the helper contract."""


class NotifySendError(NotifyError):
    """Raised when SES rejects the message or the request fails."""


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _require_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NotifyValidationError(f"{field} must be a non-empty string.")
    return value.strip()


def _require_address(value: Any, *, field: str) -> str:
    address = _require_text(value, field=field)
    local, sep, domain = address.partition("@")
    # Header injection guard: an address must be a single bare mailbox.
    if not sep or not local or "." not in domain or any(
        char in address for char in " \r\n,;<>"
    ):
        raise NotifyValidationError(f"{field} must be a single email address.")
    return address


def _resolve_dry_run(dry_run: bool | None) -> bool:
    if dry_run is not None:
        if not isinstance(dry_run, bool):
            raise NotifyConfigurationError("dry_run must be a boolean or None.")
        return dry_run
    raw = os.getenv("NOTIFY_DRY_RUN", "1").strip().lower()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    raise NotifyConfigurationError(
        "NOTIFY_DRY_RUN must be one of: 1, 0, true, false, yes, no, on, off."
    )


def dry_run_enabled() -> bool:
    """Return True when NOTIFY_DRY_RUN makes send_email log instead of send."""

    return _resolve_dry_run(None)


def _create_ses_client(region_name: str | None) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "sesv2",
        region_name=resolve_region(region_name, "SES_REGION"),
        config=Config(
            connect_timeout=3,
            read_timeout=10,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )


def send_email(
    to: str,
    subject: str,
    body: str,
    *,
    from_email: str | None = None,
    client: Any | None = None,
    dry_run: bool | None = None,
    region_name: str | None = None,
) -> dict[str, Any]:
    """Send one plain-text email through SES v2, or print it in dry-run mode.

    Returns ``{"status": "dry_run" | "sent", "to", "subject", "message_id",
    "sent_at"}``. Raises ``NotifyError`` subclasses on invalid input,
    configuration, or SES failure so agent tools can surface the failure.
    """

    recipient = _require_address(to, field="to")
    normalized_subject = _require_text(subject, field="subject")
    if "\r" in normalized_subject or "\n" in normalized_subject:
        raise NotifyValidationError("subject must be a single line.")
    if len(normalized_subject) > MAX_SUBJECT_LENGTH:
        raise NotifyValidationError(
            f"subject must be {MAX_SUBJECT_LENGTH} characters or fewer."
        )
    normalized_body = _require_text(body, field="body")

    result: dict[str, Any] = {
        "status": "dry_run",
        "to": recipient,
        "subject": normalized_subject,
        "message_id": None,
        "sent_at": _utc_timestamp(),
    }

    if _resolve_dry_run(dry_run):
        print(
            json.dumps(
                {"event": "email", **result, "body": normalized_body},
                ensure_ascii=True,
                sort_keys=True,
            ),
            flush=True,
        )
        return result

    sender = _require_address(
        from_email or os.getenv("FROM_EMAIL"),
        field="FROM_EMAIL",
    )
    active_client = client if client is not None else _create_ses_client(region_name)
    try:
        response = active_client.send_email(
            FromEmailAddress=sender,
            Destination={"ToAddresses": [recipient]},
            Content={
                "Simple": {
                    "Subject": {"Data": normalized_subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": normalized_body, "Charset": "UTF-8"}},
                }
            },
        )
    except Exception as exc:
        raise NotifySendError("Amazon SES send_email failed.") from exc

    message_id = response.get("MessageId") if isinstance(response, dict) else None
    if not isinstance(message_id, str) or not message_id:
        raise NotifySendError("Amazon SES response is missing MessageId.")

    return {**result, "status": "sent", "message_id": message_id}
