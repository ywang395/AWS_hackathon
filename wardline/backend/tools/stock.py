"""Local inventory lookup used by the Wardline routing agent."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# Resolve the fixture from this file so the lookup also works outside the repo root.
INVENTORY_PATH = Path(__file__).resolve().parents[2] / "data" / "inventory.json"


class StockError(RuntimeError):
    """Base exception for inventory lookup failures."""


class StockDataError(StockError):
    """Raised when the inventory fixture is missing or malformed."""


class StockValidationError(StockError):
    """Raised when a medication lookup input is invalid."""


def _require_non_empty_string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StockDataError(f"Inventory field '{field}' must be a non-empty string.")
    return value.strip()


def _require_number(value: Any, *, field: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StockDataError(f"Inventory field '{field}' must be numeric.")
    return value


def _load_inventory(path: Path) -> list[dict[str, Any]]:
    # Read on each call so fixture updates are visible without restarting Python.
    try:
        raw_inventory = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StockDataError(f"Unable to read inventory file: {path}.") from exc

    try:
        inventory = json.loads(raw_inventory)
    except json.JSONDecodeError as exc:
        raise StockDataError(f"Inventory file is not valid JSON: {path}.") from exc

    # The local fixture must be a list of inventory records.
    if not isinstance(inventory, list):
        raise StockDataError("Inventory file must contain a JSON list.")

    validated: list[dict[str, Any]] = []
    # Validate every record before any value reaches routing logic.
    for index, item in enumerate(inventory):
        if not isinstance(item, Mapping):
            raise StockDataError(f"Inventory item {index} must be an object.")

        location = _require_non_empty_string(
            item.get("location"),
            field="location",
        )
        med = _require_non_empty_string(item.get("med"), field="med")
        qty = _require_number(item.get("qty"), field="qty")
        distance_rank = _require_number(
            item.get("distance_rank"),
            field="distance_rank",
        )

        validated.append(
            {
                "location": location,
                "med": med,
                "qty": qty,
                "distance_rank": distance_rank,
            }
        )

    return validated


def find_stock(med: str) -> list[dict[str, Any]]:
    """Return positive-stock locations for a medication, nearest first."""

    if not isinstance(med, str) or not med.strip():
        raise StockValidationError("med must be a non-empty string.")

    # Normalize input so " insulin " and "INSULIN" match the same records.
    normalized_med = med.strip().casefold()
    # Only positive "quantities" can satisfy a medication request.
    available = [
        item
        for item in _load_inventory(INVENTORY_PATH)
        if item["med"].casefold() == normalized_med and item["qty"] > 0
    ]
    # Prefer the nearest location; use the location name to make ties stable.
    available.sort(
        key=lambda item: (
            item["distance_rank"],
            item["location"].casefold(),
        )
    )

    # Expose only the fields the routing agent needs.
    return [
        {
            "location": item["location"],
            "qty": item["qty"],
            "distance_rank": item["distance_rank"],
        }
        for item in available
    ]
