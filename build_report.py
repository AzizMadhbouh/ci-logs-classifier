#!/usr/bin/env python3
"""Generate a failure report for a single Jenkins build.

Usage (PowerShell):
    python build_report.py 20   # report for build 20
    python build_report.py 21   # report for build 21

The script reads .env for DB credentials, queries the build's issues,
and writes a human-readable report to build_<N>_report.txt in the project folder.
"""

import argparse
import os
import sys
from pathlib import Path

# 1. Load .env FIRST, before any other imports that need env vars
env_path = Path(__file__).resolve().parent / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)

# 2. Add project root to sys.path so imports work regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyze_history import get_db_conn


def build_report(build_number: int, output_path: Path) -> None:
    """Fetch one build and write a detailed failure report to *output_path*."""
    conn = get_db_conn()
    cur = conn.cursor()

    # ---- 1. Basic build metadata ----
    cur.execute(
        "SELECT build_id, result, category, llm_severity, severity_source, severity_reason "
        "FROM builds WHERE build_id = %s",
        (build_number,),
    )
    row = cur.fetchone()
    if row is None:
        print(f"Build {build_number} not found in DB.")
        return

    bid, result, cat, sev, src, reason = row

    # ---- 2. Pull the structured issues for this build ----
    cur.execute(
        "SELECT severity, category, line, is_error "
        "FROM build_issues WHERE build_id = %s ORDER BY is_error DESC, line",
        (build_number,),
    )
    issues = cur.fetchall()

    errors = [i for i in issues if i[3]]   # is_error=True
    warnings = [i for i in issues if not i[3]]

    # ---- 3. Write the report -------------------------------
    lines: list[str] = []
    lines.append(f"=== Jenkins Build Report #{bid} ===")
    lines.append(f"Result      : {result}")
    lines.append(f"Category    : {cat}")
    lines.append(f"LLM Severity: {sev}  (source: {src})")
    lines.append(f"Reason      : {reason}")
    lines.append(f"")
    lines.append(f"Error count : {len(errors)}")
    lines.append(f"Warning count: {len(warnings)}")
    lines.append(f"")

    # Helper to truncate long log lines
    trunc = lambda s: (s[:120] + "…") if len(s) > 120 else s

    lines.append("--- Errors (is_error=TRUE) ---")
    for sev_i, cat_i, line, is_err in errors:
        lines.append(f"  [{sev_i}] {cat_i} -> {trunc(line)}")
    lines.append("")

    lines.append("--- Warnings (is_error=FALSE) ---")
    for sev_i, cat_i, line, is_err in warnings:
        lines.append(f"  [{sev_i}] {cat_i} -> {trunc(line)}")
    lines.append("")

    lines.append("=== End of Report ===")

    # Write to file (UTF-8 so emojis / special chars survive)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Report written to {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a failure report for a single Jenkins build.")
    parser.add_argument("build_number", type=int, help="Jenkins build number to report (e.g. 20 or 21)")
    parser.add_argument(
        "--output",
        default=None,
        help="Optional output file path. Defaults to build_<N>_report.txt in the project folder.",
    )
    args = parser.parse_args()

    build_n = args.build_number
    out_file = Path(args.output) if args.output else Path(__file__).resolve().parent / f"build_{build_n}_report.txt"
    build_report(build_n, out_file)


if __name__ == "__main__":
    main()