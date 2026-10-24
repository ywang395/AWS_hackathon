"""
scripts/seed.py -- load the synthetic fixtures in `data/*.json` into the
Wardline DynamoDB tables (CLAUDE.md section 5).

This is explicitly a *Phase 2* script (CLAUDE.md section 9): it assumes
Lin's `template.yaml` has already created the `Nurses`, `Patients`, and
`Inventory` tables (region `us-west-2` per CLAUDE.md section 3). Phase 1
code (`tools/patients.py`, `shared/auth.py`, etc.) reads these same JSON
files directly and does not need this script to run at all.

Usage:
    python scripts/seed.py                        # seed everything in data/
    python scripts/seed.py --only nurses.json       # seed just one table
    python scripts/seed.py --dry-run                # print what would be written, no AWS call

Table names are overridable via env vars so this script keeps working if
`template.yaml` ever names tables differently in a given stage/account:
    NURSES_TABLE    (default "Nurses")
    PATIENTS_TABLE  (default "Patients")
    INVENTORY_TABLE (default "Inventory")

Inventory convention (CLAUDE.md section 5, composite key location+med):
`distance_rank` 1 = an on-unit cabinet (closest), 2 = Central Pharmacy
(farther but typically better stocked). The routing agent (Sunny's
`tools/stock.py`) should prefer the lowest `distance_rank` with qty > 0.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

DATA_DIR = Path(__file__).resolve().parents[1] / "data"

# Maps each fixture file to (env var for the table name override, default
# table name). Keeps this script in sync with CLAUDE.md section 5's table
# names without hardcoding them if a teammate renames something in
# template.yaml -- change the env var or default here, nothing else.
FIXTURES: dict[str, tuple[str, str]] = {
    "nurses.json": ("NURSES_TABLE", "Nurses"),
    "patients.json": ("PATIENTS_TABLE", "Patients"),
    "inventory.json": ("INVENTORY_TABLE", "Inventory"),
}


def _table_name(fixture_file: str) -> str:
    env_var, default = FIXTURES[fixture_file]
    return os.environ.get(env_var, default)


def _load_fixture(fixture_file: str) -> list[dict[str, Any]]:
    path = DATA_DIR / fixture_file
    with open(path, "r", encoding="utf-8") as fh:
        items = json.load(fh)
    if not isinstance(items, list):
        raise ValueError(f"{fixture_file} must contain a JSON array of items")
    return items


def seed_table(fixture_file: str, *, dry_run: bool = False, dynamodb=None) -> int:
    """
    Write every item in `data/<fixture_file>` to its DynamoDB table via
    `batch_writer()` (handles the 25-item batch limit and retries for
    us). Returns the number of items written (or that would be written,
    if `dry_run`).
    """
    items = _load_fixture(fixture_file)
    table_name = _table_name(fixture_file)

    if dry_run:
        print(f"[dry-run] would write {len(items)} items to table '{table_name}'")
        return len(items)

    if dynamodb is None:
        import boto3  # local import: only needed when actually talking to AWS

        region = os.environ.get("AWS_REGION", "us-east-1")
        dynamodb = boto3.resource("dynamodb", region_name=region)

    table = dynamodb.Table(table_name)
    with table.batch_writer() as batch:
        for item in items:
            batch.put_item(Item=item)

    print(f"seeded {len(items)} items into '{table_name}' from {fixture_file}")
    return len(items)


def seed_all(*, only: Optional[list[str]] = None, dry_run: bool = False) -> None:
    targets = only or list(FIXTURES.keys())
    total = 0
    for fixture_file in targets:
        if fixture_file not in FIXTURES:
            print(f"skip: no table mapping for '{fixture_file}'", file=sys.stderr)
            continue
        try:
            total += seed_table(fixture_file, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 - keep going, report, continue other tables
            # Covers the "ran this before template.yaml was deployed" case
            # (CLAUDE.md section 9, Lin's Phase 1 checklist item "sam
            # deploy the empty stack successfully") -- a missing table
            # shows up here as a ResourceNotFoundException from boto3.
            print(f"error seeding '{fixture_file}': {exc}", file=sys.stderr)
    print(f"done - {total} item(s) processed")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        nargs="+",
        choices=list(FIXTURES.keys()),
        help="seed only these fixture files (default: all of them)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be written without calling AWS",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    seed_all(only=args.only, dry_run=args.dry_run)