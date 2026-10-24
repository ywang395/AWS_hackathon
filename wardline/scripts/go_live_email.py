"""
scripts/go_live_email.py -- Phase 2 box: "Turn off email dry-run; test with
real (SES-verified) inboxes" (CLAUDE.md section 9).

The email path is already built: shared/notify.py sends through SES v2 and
the alert agent calls it. This script is the go-live glue -- it turns off
NOTIFY_DRY_RUN, sends one test email, then (by default) fires the alert
agent's OutageDetected sweep so the 4 on-shift nurses are emailed exactly as
they would be in the demo.

While SES is in sandbox mode it only delivers to verified addresses, and the
seed data uses synthetic @example.com addresses. NOTIFY_TO_OVERRIDE
(live_config.py) redirects EVERY message to one verified inbox, so the whole
sweep lands there without putting real addresses in data/.

Run after logging into the workshop account (CLAUDE.md section 9):
    python scripts/go_live_email.py --test-only   # one test email
    python scripts/go_live_email.py               # test email + outage sweep

Needs boto3 and credentials with SES send permission. FROM_EMAIL must be a
verified sender and NOTIFY_TO_OVERRIDE a verified recipient (live_config.py).
Uses an in-memory ack store so the real Alerts table is untouched.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.shared.config import load_default_env  # noqa: E402
from scripts import live_config  # noqa: E402


def _configure() -> None:
    """Apply live email settings for this process after the placeholders check out."""
    # Validate BEFORE touching os.environ, so a leftover placeholder cannot
    # leave NOTIFY_DRY_RUN=0 behind when require() raises SystemExit.
    sender = live_config.require("FROM_EMAIL", live_config.FROM_EMAIL)
    # REQUIRED in sandbox: redirect every message to one verified inbox.
    recipient = live_config.require("NOTIFY_TO_OVERRIDE", live_config.NOTIFY_TO_OVERRIDE)
    os.environ["FROM_EMAIL"] = sender
    os.environ["NOTIFY_TO_OVERRIDE"] = recipient
    os.environ["NOTIFY_DRY_RUN"] = "0"  # the whole point of this box: real sends
    os.environ.setdefault("AWS_REGION", live_config.AWS_REGION)
    os.environ.setdefault("FRONTEND_BASE_URL", live_config.FRONTEND_BASE_URL)


def _send_test_email() -> int:
    from backend.shared.notify import send_email

    result = send_email(
        os.environ["NOTIFY_TO_OVERRIDE"],
        "Wardline SES test",
        "If you received this, the Wardline SES path works end to end.",
    )
    print(f"[{result.status}] test email -> {result.delivered_to} (MessageId={result.message_id})")
    return 0 if result.status == "sent" else 1


def _run_outage_sweep() -> int:
    """Fire the alert agent's OutageDetected sweep, emailing every on-shift nurse.

    Uses a throwaway in-memory ack store so this test does not write to the
    real Alerts table. In production the EventBridge-triggered handler uses
    the DynamoDB store instead.
    """
    from backend.agents.alert_agent import app as alert

    outcomes = alert.run_ack_sweep(store=alert.InMemoryAckStore(), token_store=alert.auth.InMemoryTokenStore())
    sent = [o for o in outcomes if o.get("notify_status") == "sent"]
    for o in outcomes:
        print(f"  {o['nurse_id']}: action={o['action']} notify={o.get('notify_status')}")
    print(f"outage sweep: {len(sent)}/{len(outcomes)} emails sent")
    # Every on-shift nurse/charge must have been emailed for the box to pass.
    return 0 if outcomes and len(sent) == len(outcomes) else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Send real SES email for the Phase 2 go-live check.")
    parser.add_argument("--test-only", action="store_true", help="send one test email, skip the outage sweep")
    args = parser.parse_args(argv)

    _configure()
    print(f"NOTIFY_DRY_RUN=0  FROM_EMAIL={os.environ['FROM_EMAIL']}  -> {os.environ['NOTIFY_TO_OVERRIDE']}")

    failures = _send_test_email()
    if not args.test_only:
        failures += _run_outage_sweep()
    print("EMAIL GO-LIVE PASSED" if failures == 0 else f"{failures} STEP(S) FAILED")
    return failures


if __name__ == "__main__":
    # Load wardline/.env first (the routing_agent/cli.py convention), then re-read
    # live_config, which reads os.environ when it is imported.
    load_default_env()
    importlib.reload(live_config)
    sys.exit(1 if main(sys.argv[1:]) else 0)