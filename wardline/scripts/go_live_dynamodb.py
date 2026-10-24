"""
scripts/go_live_dynamodb.py -- Phase 2 box: "Switch tools from JSON to
DynamoDB; run seed" (CLAUDE.md section 9).

The tool switch is already built: shared/db.py reads DATA_BACKEND, and
auth/patients/downtime_log/alert all have a DynamoDB path. This script is
the go-live glue that was missing -- it sets DATA_BACKEND=dynamodb, seeds
the tables, and reads back through the real tools to prove the switch works
against live DynamoDB.

Run after logging into the workshop account (CLAUDE.md section 9):
    python scripts/go_live_dynamodb.py --dry-run   # seed counts only, no reads
    python scripts/go_live_dynamodb.py             # seed + live read-back checks

Needs boto3 and credentials for the account. The data-plane tables
(template.yaml) must already be deployed; a missing table surfaces as a
ResourceNotFoundException naming it.
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
from scripts import live_config  # noqa: E402  (also runnable as `python scripts/go_live_dynamodb.py`)
import scripts.seed as seed  # noqa: E402


def _configure() -> None:
    """Point the backend at DynamoDB in the configured region, for this process."""
    os.environ["DATA_BACKEND"] = "dynamodb"
    os.environ.setdefault("AWS_REGION", live_config.AWS_REGION)


def verify() -> int:
    """Read back through the switched tools. Returns the number of failed checks.

    TODO(go-live): extend as more tools move to DynamoDB. Sunny's
    tools/deliveries.py still uses an in-memory store, so /chat shows no
    deliveries on DynamoDB until its create_delivery/list writes to the
    Deliveries table. Add a delivery read-back here once that lands.
    """
    from backend.shared import auth, db
    from backend.tools.patients import PatientAccessDenied, get_my_patients, get_patient

    failures = 0

    def check(label: str, ok: bool) -> None:
        nonlocal failures
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
        failures += 0 if ok else 1

    check("DATA_BACKEND resolves to dynamodb", db.use_dynamodb())

    # Patients come back scoped to the nurse, through the GSI.
    kim_patients = get_my_patients("N002")
    check("get_my_patients('N002') returns 4 seeded rooms",
          sorted(p["room"] for p in kim_patients) == ["301", "302", "303", "304"])

    # Float survived the Decimal round-trip (the seed's main risk).
    p304 = db.get_item("patients", patient_id="P304")
    check("P304 vitals.temp_f round-trips as a float", isinstance(p304["vitals"]["temp_f"], float))

    # Ownership check still refuses another nurse's room.
    try:
        get_patient("N002", "305")
        check("room 305 refused to N002", False)
    except PatientAccessDenied:
        check("room 305 refused to N002", True)

    # Token mint + PIN verify both go through DynamoDB (then clean up the test token).
    token = auth.create_token("N002")
    try:
        check("token+PIN authenticate via DynamoDB", auth.authenticate(token, "1234") == "N002")
    finally:
        db.delete_item("tokens", token=token)

    return failures


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Seed DynamoDB and verify the tool switch.")
    parser.add_argument("--dry-run", action="store_true", help="seed counts only; skip live read-back")
    args = parser.parse_args(argv)

    _configure()
    print(f"DATA_BACKEND=dynamodb  AWS_REGION={os.environ['AWS_REGION']}")

    seed_failures = seed.seed_all(dry_run=args.dry_run)
    if args.dry_run:
        return seed_failures
    if seed_failures:
        print("seed reported failures; skipping read-back.", file=sys.stderr)
        return seed_failures

    print("--- read-back checks ---")
    failures = verify()
    print("ALL CHECKS PASSED" if failures == 0 else f"{failures} CHECK(S) FAILED")
    return failures


if __name__ == "__main__":
    # Load wardline/.env first (the routing_agent/cli.py convention), then re-read
    # live_config, which reads os.environ when it is imported.
    load_default_env()
    importlib.reload(live_config)
    sys.exit(1 if main(sys.argv[1:]) else 0)