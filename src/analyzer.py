"""Sales data analysis functions."""

from collections import defaultdict


def calculate_total_sales(data: list[dict]) -> float:
    """Calculate total revenue from all sales."""
    total = 0.0
    for row in data:
        total += row["quantity"] * row["price"]
    return round(total, 2)


def calculate_average_order_value(data: list[dict]) -> float:
    """Calculate average revenue per transaction."""
    if not data:
        return 0.0

    total = calculate_total_sales(data)
    return round(total / len(data), 2)


def get_top_products(data: list[dict], n: int = 3) -> list[tuple[str, float]]:
    """Get top N products by total revenue.

    Returns list of (product_name, total_revenue) tuples.
    """
    product_revenue: defaultdict[str, float] = defaultdict(float)

    for row in data:
        revenue = row["quantity"] * row["price"]
        product_revenue[row["product"]] += revenue

    sorted_products = sorted(
        product_revenue.items(),
        key=lambda x: x[1],
        reverse=True,
    )

    return sorted_products[:n]


def get_daily_sales(data: list[dict]) -> dict[str, float]:
    """Group sales by date and calculate daily totals."""
    daily: defaultdict[str, float] = defaultdict(float)

    for row in data:
        revenue = row["quantity"] * row["price"]
        daily[row["date"]] += revenue

    return {k: round(v, 2) for k, v in sorted(daily.items())}


def get_quantity_by_product(data: list[dict]) -> dict[str, int]:
    """Get total quantity sold per product."""
    quantities: defaultdict[str, int] = defaultdict(int)

    for row in data:
        quantities[row["product"]] += row["quantity"]

    return dict(sorted(quantities.items()))
