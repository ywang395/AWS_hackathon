"""
scripts/live_config.py -- placeholder settings for the two Phase 2 go-live
steps (CLAUDE.md section 9):

    - Switch tools from JSON to DynamoDB; run seed
    - Turn off email dry-run; test with real (SES-verified) inboxes

HOW TO USE
    Every value below is a dummy marked `# TODO(go-live)`. An environment
    variable of the SAME NAME overrides it, so you can either edit this file
    or export the vars before running the scripts.

    Pre-filled with what CLAUDE.md section 9 documents (region us-east-1,
    SES sender lintuwun@gmail.com) so the scripts run without edits. The one
    value you MUST set is a real SES-verified recipient: the seed data uses
    synthetic @example.com addresses (CLAUDE.md section 2) that SES cannot
    deliver to in the sandbox.

    Credentials are never read here. Log in through the event portal first
    (CLAUDE.md section 9) so boto3 picks up the temporary credentials from
    the environment / ~/.aws. They expire -- re-login when a session lapses.

SAFETY
    require() refuses any value still left at its CHANGE_ME placeholder, so a
    script cannot send real email to a fake address or seed the wrong account
    by accident.
"""

from __future__ import annotations

import os

# Marker for a value that must be replaced before the live scripts will run.
_PLACEHOLDER_PREFIX = "CHANGE_ME"


# --- AWS account / region -------------------------------------------------
# TODO(go-live): confirm this matches the account you logged into.
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
EXPECTED_ACCOUNT_ID = os.environ.get("EXPECTED_ACCOUNT_ID", "580066280520")  # TODO(go-live): workshop account


# --- Email (SES) ----------------------------------------------------------
# TODO(go-live): FROM_EMAIL must be an SES-verified sender identity.
FROM_EMAIL = os.environ.get("FROM_EMAIL", "lintuwun@gmail.com")

# TODO(go-live): REQUIRED. An SES-verified inbox that receives every demo
# email while SES is in sandbox mode. CLAUDE.md lists lintu.wang@sjsu.edu as
# verified -- set NOTIFY_TO_OVERRIDE to it (or your own verified address).
NOTIFY_TO_OVERRIDE = os.environ.get("NOTIFY_TO_OVERRIDE", f"{_PLACEHOLDER_PREFIX}-verified-inbox@example.com")

# TODO(go-live): base URL of the hosted frontend, so the secure link in the
# alert email opens NurseView. Until the frontend is hosted (waits on the
# section 3 compute decision), the link points at this placeholder host and
# will not resolve -- the email still sends and the token is still valid.
FRONTEND_BASE_URL = os.environ.get("FRONTEND_BASE_URL", "https://wardline.example/nurse")


def is_placeholder(value: str) -> bool:
    return value.startswith(_PLACEHOLDER_PREFIX)


def require(name: str, value: str) -> str:
    """Return value, or exit if it is still a CHANGE_ME placeholder."""
    if is_placeholder(value):
        raise SystemExit(
            f"{name} is still the placeholder '{value}'.\n"
            f"Set it in scripts/live_config.py or export {name} before running this script."
        )
    return value