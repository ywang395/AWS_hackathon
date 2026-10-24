"""
scripts/reset.py -- clear Wardline's demo/runtime state so the demo can be
rerun cleanly (CLAUDE.md section 10, M4).

Clears the tables that accumulate during a run -- logs, deliveries, alerts,
escalations, the live agent feed, chat history, and secure-link tokens --
and flips SystemState back to 'up'. It does NOT touch the seed data
(Nurses, Patients, Inventory): those are reloaded with scripts/seed.py, not
reset here.

Usage (from the wardline/ folder):
    python scripts/reset.py              # clear all runtime tables
    python scripts/reset.py --dry-run    # count what would be deleted, no writes
    python scripts/reset.py --only Deliveries Alerts

Table names and region come from backend/shared/db.py (DATA_BACKEND must be
dynamodb and the data plane deployed). Safe to run repeatedly.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # run as a plain script: make `backend` importable

from backend.shared import db  # noqa: E402
from backend.shared.config import load_default_env  # noqa: E402

# Logical table key (shared/db.py TABLES) -> the key attributes to delete by.
# These are the runtime tables that fill up during a demo; seed tables are
# intentionally excluded so a reset never wipes nurses/patients/inventory.
RUNTIME_TABLES: dict[str, list[str]] = {
    "downtime_log": ["patient_id", "ts"],
    "deliveries": ["request_id"],
    "alerts": ["nurse_id", "sent_at"],
    "escalations": ["escalation_id"],
    "agent_feed": ["day", "ts"],
    "chat_history": ["token", "ts"],
    "tokens": ["token"],
}


def clear_table(table_key: str, key_attrs: list[str], *, dry_run: bool = False) -> int:
    """Delete every item from one runtime table. Returns the item count."""
    name = db.table_name(table_key)
    items = db.scan(table_key)
    if dry_run:
        print(f"[dry-run] would delete {len(items)} items from '{name}'")
        return len(items)

    table = db.get_table(table_key)
    with table.batch_writer() as batch:
        for item in items:
            batch.delete_item(Key={attr: item[attr] for attr in key_attrs})
    print(f"cleared {len(items)} items from '{name}'")
    return len(items)


def reset_system_state(*, dry_run: bool = False) -> None:
    """Flip SystemState (PK id='hospital') back to 'up' for a fresh demo."""
    name = db.table_name("system_state")
    if dry_run:
        print(f"[dry-run] would reset '{name}' id=hospital to status=up")
        return
    db.put_item("system_state", {"id": "hospital", "status": "up"})
    print(f"reset '{name}' id=hospital to status=up")


def reset_all(*, only: list[str] | None = None, dry_run: bool = False) -> int:
    """Clear each runtime table and reset SystemState.

    `only` filters by DynamoDB table name (e.g. 'Deliveries'), matching what
    a user sees in the console. One failing table does not stop the rest.
    """
    failures = 0
    for table_key, key_attrs in RUNTIME_TABLES.items():
        if only and db.table_name(table_key) not in only:
            continue
        try:
            clear_table(table_key, key_attrs, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 - report and continue
            failures += 1
            print(f"error clearing '{db.table_name(table_key)}': {exc}", file=sys.stderr)

    if not only or db.table_name("system_state") in only:
        try:
            reset_system_state(dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"error resetting SystemState: {exc}", file=sys.stderr)

    # In-memory tool stores (used when DATA_BACKEND=json, or for the delivery/
    # escalation stores that are still process-local): clear them too so an
    # in-process server starts a rerun clean without a restart.
    _reset_in_memory_stores()

    print(f"reset complete - {failures} table(s) failed")
    return failures


def _reset_in_memory_stores() -> None:
    """Clear the process-local tool stores (tools/deliveries, tools/escalation)."""
    try:
        from backend.tools import deliveries, escalation

        deliveries.reset_deliveries()
        if hasattr(escalation, "reset_escalations"):
            escalation.reset_escalations()
    except Exception as exc:  # noqa: BLE001
        print(f"note: could not reset in-memory stores: {exc}", file=sys.stderr)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    table_names = sorted({__table_name(k) for k in RUNTIME_TABLES} | {"SystemState"})
    parser = argparse.ArgumentParser(description="Clear Wardline runtime tables for a clean demo rerun.")
    parser.add_argument("--only", nargs="+", choices=table_names, help="reset only these tables (by table name)")
    parser.add_argument("--dry-run", action="store_true", help="count items without deleting")
    return parser.parse_args(argv)


def __table_name(key: str) -> str:
    # Resolve at arg-parse time so --only choices match the real (env-overridable) names.
    return db.table_name(key)


if __name__ == "__main__":
    load_default_env()  # wardline/.env, same convention as seed.py
    args = _parse_args(sys.argv[1:])
    sys.exit(1 if reset_all(only=args.only, dry_run=args.dry_run) else 0)
