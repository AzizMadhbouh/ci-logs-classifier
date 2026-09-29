"""Report generation for sales data."""

import csv
from .analyzer import (
    calculate_average_order_value,
    calculate_total_sales,
    get_daily_sales,
    get_quantity_by_product,
    get_top_products,
)


def generate_text_report(data: list[dict]) -> str:
    """Generate a plain text summary report."""
    total = calculate_total_sales(data)
    avg = calculate_average_order_value(data)
    top = get_top_products(data, n=3)
    daily = get_daily_sales(data)
    quantities = get_quantity_by_product(data)

    lines = [
        "=" * 50,
        "       SALES REPORT",
        "=" * 50,
        "",
        "--- Summary ---",
        f"Total Revenue:    ${total:,.2f}",
        f"Total Orders:     {len(data)}",
        f"Avg Order Value:  ${avg:,.2f}",
        "",
        "--- Top 3 Products by Revenue ---",
    ]

    for i, (product, revenue) in enumerate(top, 1):
        lines.append(f"  {i}. {product:20s} ${revenue:,.2f}")

    lines.append("")
    lines.append("--- Daily Sales ---")

    for date, revenue in daily.items():
        lines.append(f"  {date:12s} ${revenue:,.2f}")

    lines.append("")
    lines.append("--- Units Sold by Product ---")

    for product, qty in quantities.items():
        lines.append(f"  {product:20s} {qty} units")

    lines.append("")
    lines.append("=" * 50)

    return "\n".join(lines)


def generate_csv_report(data: list[dict], output_path: str) -> None:
    """Export analyzed data to a CSV file."""
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["date", "product", "quantity", "price", "revenue"]
        )
        writer.writeheader()

        for row in data:
            writer.writerow(
                {
                    "date": row["date"],
                    "product": row["product"],
                    "quantity": row["quantity"],
                    "price": row["price"],
                    "revenue": round(row["quantity"] * row["price"], 2),
                }
            )
