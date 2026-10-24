"""
scripts/seed.py -- load the synthetic fixtures in data/*.json into the
Wardline DynamoDB tables (CLAUDE.md section 5).

Needs Lin's template.yaml data plane deployed (Nurses, Patients, Inventory
tables) in the region from CLAUDE.md section 3 (us-east-1), plus AWS
credentials for the account. Phase 1 code reads these same JSON files
directly, so nothing else depends on this script having run.

Usage (from the wardline/ folder):
    python scripts/seed.py                       # seed every fixture
    python scripts/seed.py --only nurses.json    # seed one table
    python scripts/seed.py --dry-run             # count items, no AWS call

Table names and region come from backend/shared/db.py, so the NURSES_TABLE /
PATIENTS_TABLE / INVENTORY_TABLE and AWS_REGION env vars that the backend
uses apply here too.

Floats (vitals such as temp_f) are converted to Decimal before writing:
boto3 rejects Python floats, so a raw json.load() would fail on Patients.

Inventory convention (section 5, composite key location + med):
distance_rank 1 is an on-unit cabinet, 2 is Central Pharmacy. The routing
agent's tools/stock.py prefers the lowest rank with qty > 0.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # run as a plain script: make `backend` importable

from backend.shared import db  # noqa: E402

DATA_DIR = ROOT / "data"

# Fixture file -> logical table key in shared/db.py TABLES.
FIXTURES: dict[str, str] = {
    "nurses.json": "nurses",
    "patients.json": "patients",
    "inventory.json": "inventory",
}


def _load_fixture(fixture_file: str) -> list[dict[str, Any]]:
    items = json.loads((DATA_DIR / fixture_file).read_text(encoding="utf-8"))
    if not isinstance(items, list):
        raise ValueError(f"{fixture_file} must contain a JSON array of items")
    return items


def seed_table(fixture_file: str, *, dry_run: bool = False) -> int:
    """Write every item in data/<fixture_file> to its table. Returns the item count.

    batch_writer() handles the 25-item batch limit and retries unprocessed
    items. Re-running is safe: put_item overwrites rows with the same key.
    """
    items = _load_fixture(fixture_file)
    name = db.table_name(FIXTURES[fixture_file])
    if dry_run:
        print(f"[dry-run] would write {len(items)} items to table '{name}'")
        return len(items)

    if dynamodb is None:
        import boto3  # local import: only needed when actually talking to AWS

        region = os.environ.get("AWS_REGION", "us-east-1")
        dynamodb = boto3.resource("dynamodb", region_name=region)

    table = dynamodb.Table(table_name)
    with table.batch_writer() as batch:
        for item in items:
            batch.put_item(Item=db.to_dynamo(item))
    print(f"seeded {len(items)} items into '{name}' from {fixture_file}")
    return len(items)


def seed_all(*, only: Optional[list[str]] = None, dry_run: bool = False) -> int:
    """Seed each fixture. One failing table (e.g. not deployed yet) does not stop the others."""
    total = 0
    failures = 0
    for fixture_file in only or list(FIXTURES):
        try:
            total += seed_table(fixture_file, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 - report and continue with the other tables
            # A missing table shows up here as ResourceNotFoundException: deploy template.yaml first.
            failures += 1
            print(f"error seeding '{fixture_file}': {exc}", file=sys.stderr)
    print(f"done - {total} item(s) processed, {failures} table(s) failed")
    return failures


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed Wardline DynamoDB tables from data/*.json.")
    parser.add_argument("--only", nargs="+", choices=list(FIXTURES), help="seed only these fixture files")
    parser.add_argument("--dry-run", action="store_true", help="count items without calling AWS")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    sys.exit(1 if seed_all(only=args.only, dry_run=args.dry_run) else 0)