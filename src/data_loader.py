"""Data loading and validation for sales data."""

import csv
from pathlib import Path


def load_csv(file_path: str) -> list[dict]:
    """Load a CSV file and return a list of dictionaries."""
    path = Path(file_path)

    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    if path.suffix != ".csv":
        raise ValueError(f"Expected CSV file, got: {path.suffix}")

    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


def validate_sales_data(data: list[dict]) -> list[dict]:
    """Validate and clean sales data.

    Expected columns: date, product, quantity, price
    """
    required_columns = {"date", "product", "quantity", "price"}

    if not data:
        raise ValueError("Data is empty")

    actual_columns = set(data[0].keys())
    missing = required_columns - actual_columns

    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    cleaned = []
    for row in data:
        try:
            cleaned.append(
                {
                    "date": row["date"],
                    "product": row["product"],
                    "quantity": int(row["quantity"]),
                    "price": float(row["price"]),
                }
            )
        except (ValueError, KeyError):
            continue

    return cleaned
