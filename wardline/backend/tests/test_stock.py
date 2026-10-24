"""Unit tests for local inventory lookup."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from backend.tools import stock
from backend.tools.stock import (
    StockDataError,
    StockValidationError,
    find_stock,
)


def write_inventory(
    path: Path,
    inventory: Any,
) -> None:
    path.write_text(json.dumps(inventory), encoding="utf-8")


def test_find_stock_returns_positive_stock_nearest_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path = tmp_path / "inventory.json"
    write_inventory(
        inventory_path,
        [
            {
                "location": "Central pharmacy",
                "med": "insulin",
                "qty": 8,
                "distance_rank": 3,
            },
            {
                "location": "Unit 4 cabinet",
                "med": "insulin",
                "qty": 0,
                "distance_rank": 1,
            },
            {
                "location": "Unit 6 cabinet",
                "med": "insulin",
                "qty": 4,
                "distance_rank": 2,
            },
        ],
    )
    monkeypatch.setattr(stock, "INVENTORY_PATH", inventory_path)

    result = find_stock("  INSULIN ")

    assert result == [
        {
            "location": "Unit 6 cabinet",
            "qty": 4,
            "distance_rank": 2,
        },
        {
            "location": "Central pharmacy",
            "qty": 8,
            "distance_rank": 3,
        },
    ]


def test_find_stock_returns_empty_list_when_medication_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path = tmp_path / "inventory.json"
    write_inventory(
        inventory_path,
        [
            {
                "location": "Central pharmacy",
                "med": "heparin",
                "qty": 0,
                "distance_rank": 1,
            }
        ],
    )
    monkeypatch.setattr(stock, "INVENTORY_PATH", inventory_path)

    assert find_stock("heparin") == []


@pytest.mark.parametrize("med", ["", "   ", None])  # type: ignore[list-item]
def test_find_stock_rejects_invalid_medication_input(
    med: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path = tmp_path / "inventory.json"
    write_inventory(inventory_path, [])
    monkeypatch.setattr(stock, "INVENTORY_PATH", inventory_path)

    with pytest.raises(StockValidationError, match="non-empty string"):
        find_stock(med)  # type: ignore[arg-type]


def test_missing_inventory_file_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path = tmp_path / "missing.json"
    monkeypatch.setattr(stock, "INVENTORY_PATH", inventory_path)

    with pytest.raises(StockDataError, match="Unable to read"):
        find_stock("insulin")


def test_malformed_inventory_json_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(stock, "INVENTORY_PATH", inventory_path)

    with pytest.raises(StockDataError, match="not valid JSON"):
        find_stock("insulin")


def test_invalid_inventory_record_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path = tmp_path / "inventory.json"
    write_inventory(
        inventory_path,
        [
            {
                "location": "Unit 6 cabinet",
                "med": "insulin",
                "qty": "four",
                "distance_rank": 2,
            }
        ],
    )
    monkeypatch.setattr(stock, "INVENTORY_PATH", inventory_path)

    with pytest.raises(StockDataError, match="qty"):
        find_stock("insulin")


def test_bundled_fixture_matches_required_demo_cases() -> None:
    assert [item["location"] for item in find_stock("insulin")] == [
        "Unit 6 cabinet"
    ]
    assert find_stock("heparin") == []
