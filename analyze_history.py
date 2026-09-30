import os
import json
import re
import psycopg2
from datetime import datetime
from pathlib import Path
from collections import defaultdict


DATA_DIR = Path("build_history")


def _load_dotenv(path=".env"):
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_dotenv()

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ.get("DB_NAME", "buildhistory")
DB_USER = os.environ.get("DB_USER", "builduser")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")


def get_db_conn():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )


def ensure_schema():
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS builds (
            id SERIAL PRIMARY KEY,
            build_id INTEGER UNIQUE,
            timestamp TIMESTAMP,
            error_count INTEGER,
            warning_count INTEGER,
            trend TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS build_issues (
            id SERIAL PRIMARY KEY,
            build_id INTEGER,
            timestamp TIMESTAMP,
            severity TEXT,
            category TEXT,
            line TEXT,
            is_error BOOLEAN
        )
    """)
    cur.execute("ALTER TABLE build_issues ADD COLUMN IF NOT EXISTS timestamp TIMESTAMP")
    cur.execute("ALTER TABLE builds ADD COLUMN IF NOT EXISTS result TEXT")
    cur.execute("ALTER TABLE builds ADD COLUMN IF NOT EXISTS llm_severity TEXT")
    cur.execute("ALTER TABLE builds ADD COLUMN IF NOT EXISTS severity_source TEXT")
    cur.execute("ALTER TABLE builds ADD COLUMN IF NOT EXISTS severity_reason TEXT")
    cur.execute("ALTER TABLE builds ADD COLUMN IF NOT EXISTS category TEXT")
    conn.commit()
    conn.close()


def save_build_to_db(data):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO builds (build_id, timestamp, error_count, warning_count, trend, result) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (build_id) DO UPDATE SET "
        "timestamp = EXCLUDED.timestamp, error_count = EXCLUDED.error_count, "
        "warning_count = EXCLUDED.warning_count, result = EXCLUDED.result",
        (
            data["build_id"],
            datetime.fromisoformat(data["timestamp"]),
            data["error_count"],
            data["warning_count"],
            data.get("trend", ""),
            data.get("result"),
        ),
    )
    row_timestamp = datetime.fromisoformat(data["timestamp"])
    for entry in data["errors"]:
        cur.execute(
            "INSERT INTO build_issues (build_id, timestamp, severity, category, line, is_error) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (data["build_id"], row_timestamp, entry["severity"], entry["category"], entry["line"], True),
        )
    for entry in data["warnings"]:
        cur.execute(
            "INSERT INTO build_issues (build_id, timestamp, severity, category, line, is_error) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (data["build_id"], row_timestamp, entry["severity"], entry["category"], entry["line"], False),
        )
    conn.commit()
    conn.close()


def update_trends():
    """Recompute trend for every build based on the full history."""
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT build_id, error_count FROM builds ORDER BY timestamp")
    rows = cur.fetchall()
    if len(rows) < 2:
        conn.close()
        return
    error_trend = [r[1] for r in rows]
    trend = "stable"
    recent = error_trend[-1]
    earlier = error_trend[0]
    if recent > earlier * 1.5:
        trend = "increasing"
    elif recent < earlier * 0.5:
        trend = "decreasing"
    cur.execute("UPDATE builds SET trend = %s", (trend,))
    conn.commit()
    conn.close()


def seed_from_history():
    if not DATA_DIR.exists():
        print("No build_history directory found.")
        return
    ensure_schema()
    for path in sorted(DATA_DIR.glob("build_*.json")):
        with open(path) as f:
            data = json.load(f)
        save_build_to_db(data)
        print(f"Seeded build {data['build_id']}: {data['error_count']} errors, {data['warning_count']} warnings")


def parse_report(text):
    """Parse analysis-report.txt into structured data."""
    errors = []
    warnings = []
    error_count = 0
    warning_count = 0

    section = None
    for line in text.split("\n"):
        line = line.strip()

        if line.startswith("Errors:"):
            error_count = int(line.split(":")[1].strip())
            section = None
        elif line.startswith("Warnings:"):
            warning_count = int(line.split(":")[1].strip())
            section = None
        elif line.startswith("Error Details:"):
            section = "errors"
        elif line.startswith("Warning Details:"):
            section = "warnings"
        elif line.startswith("[") and section:
            match = re.match(r"\[(\w+)\]\s+\[(\w+)\]\s+(.*)", line)
            if match:
                entry = {
                    "severity": match.group(1),
                    "category": match.group(2),
                    "line": match.group(3)
                }
                if section == "errors":
                    errors.append(entry)
                else:
                    warnings.append(entry)

    return {
        "error_count": error_count,
        "warning_count": warning_count,
        "errors": errors,
        "warnings": warnings
    }


def save_build(build_id, report_path, result=None):
    """Read a report file, save it to build_history/ and PostgreSQL."""
    DATA_DIR.mkdir(exist_ok=True)

    with open(report_path, encoding="utf-8-sig") as f:
        text = f.read()

    data = parse_report(text)
    data["build_id"] = build_id
    data["timestamp"] = datetime.now().isoformat()
    data["report_path"] = str(report_path)
    data["result"] = result

    out_path = DATA_DIR / f"build_{build_id}.json"
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    ensure_schema()
    save_build_to_db(data)
    update_trends()
    print(f"DB: saved build {build_id} | result={result or '?'}")

    print(f"Saved build {build_id}: {data['error_count']} errors, {data['warning_count']} warnings")
    return data


def load_all_builds():
    """Load all saved builds from build_history/."""
    builds = []
    if not DATA_DIR.exists():
        return builds

    for path in sorted(DATA_DIR.glob("build_*.json")):
        with open(path) as f:
            builds.append(json.load(f))

    return builds


def detect_patterns(builds):
    """Find patterns across all builds.

    Returns:
        recurring: issues that appear in 2+ builds
        hotspots: files with most issues
        trends: error/warning counts over time
    """
    if not builds:
        return {}, {}, {}


    issue_history = defaultdict(list)
    file_issues = defaultdict(int)
    error_trend = []
    warning_trend = []

    for build in builds:
        bid = build["build_id"]
        error_trend.append(build["error_count"])
        warning_trend.append(build["warning_count"])

        for entry in build["errors"] + build["warnings"]:
            line = entry["line"]
            issue_history[line].append(bid)


            file_match = re.match(r"([\w/]+\.py)", line)
            if file_match:
                file_issues[file_match.group(1)] += 1


    recurring = {
        line: builds
        for line, builds in issue_history.items()
        if len(builds) >= 2
    }


    hotspots = dict(sorted(file_issues.items(), key=lambda x: -x[1])[:5])


    trend = "stable"
    if len(error_trend) >= 2:
        recent = error_trend[-1]
        earlier = error_trend[0]
        if recent > earlier * 1.5:
            trend = "increasing"
        elif recent < earlier * 0.5:
            trend = "decreasing"

    return recurring, hotspots, trend


SEVERITY_WEIGHT = {
    "Critical": 5,
    "High": 4,
    "Medium": 3,
    "Low": 1,
}


BUILD_BREAKING = {"Types", "Tests", "Dependencies", "Git", "Docker", "Network"}


def score_issue(issue, frequency, age_builds):
    """Calculate priority score for an issue.

    Rules:
    1. Build-breaking errors get a HUGE bonus (they block deploys)
    2. More frequent = more urgent
    3. Higher severity = more urgent
    4. Older issue (appears across many builds) = more urgent

    score = build_bonus + (severity × 3) + (frequency × 2) + (age × 1)
    """
    build_bonus = 1000 if issue["category"] in BUILD_BREAKING else 0
    severity = SEVERITY_WEIGHT.get(issue["severity"], 1)
    score = build_bonus + (severity * 3) + (frequency * 2) + (age_builds * 1)
    return score


def analyze_all():
    """Main entry point: load all builds, detect patterns, rank issues."""
    builds = load_all_builds()
    if not builds:
        print("No builds found. Run: python analyze_history.py --save <path>")
        return

    recurring, hotspots, trend = detect_patterns(builds)
    total_builds = len(builds)


    issue_stats = defaultdict(lambda: {"errors": 0, "warnings": 0, "builds": set()})
    for build in builds:
        for entry in build["errors"]:
            line = entry["line"]
            issue_stats[line]["errors"] += 1
            issue_stats[line]["builds"].add(build["build_id"])
        for entry in build["warnings"]:
            line = entry["line"]
            issue_stats[line]["warnings"] += 1
            issue_stats[line]["builds"].add(build["build_id"])


    scored = []
    for line, stats in issue_stats.items():

        for build in reversed(builds):
            issue = next(
                (e for e in build["errors"] + build["warnings"] if e["line"] == line),
                None
            )
            if issue:
                break

        if issue is None:
            continue

        frequency = stats["errors"] + stats["warnings"]
        age = len(stats["builds"])
        score = score_issue(issue, frequency, age)

        scored.append({
            "line": line,
            "category": issue["category"],
            "severity": issue["severity"],
            "frequency": frequency,
            "builds_affected": age,
            "score": score,
            "is_error": issue["category"] in BUILD_BREAKING
        })

    scored.sort(key=lambda x: -x["score"])

    report = []
    report.append("=" * 60)
    report.append("     BUILD HISTORY ANALYSIS REPORT")
    report.append("=" * 60)
    report.append(f"\nTotal builds analyzed: {total_builds}")
    report.append(f"Error trend: {trend}")

    total_errors = sum(b["error_count"] for b in builds)
    total_warnings = sum(b["warning_count"] for b in builds)
    report.append(f"Total errors: {total_errors}")
    report.append(f"Total warnings: {total_warnings}")

    if hotspots:
        report.append("\n--- HOTSPOT FILES (most issues) ---")
        for file, count in hotspots.items():
            report.append(f"  {file}: {count} issues")

    if recurring:
        report.append(f"\n--- RECURRING ISSUES ({len(recurring)} appear in 2+ builds) ---")
        for line, blds in list(recurring.items())[:5]:
            report.append(f"  [{len(blds)} builds] {line[:70]}")

    report.append("\n--- PRIORITIZED ISSUES (highest priority first) ---")
    for i, issue in enumerate(scored[:15], 1):
        prefix = "ERROR" if issue["is_error"] else "WARN "
        report.append(
            f"  {i}. [{prefix}] [score={issue['score']}] "
            f"[{issue['severity']}/{issue['category']}] "
            f"freq={issue['frequency']}, builds={issue['builds_affected']}\n"
            f"     {issue['line'][:75]}"
        )

    report.append("\n--- PRIORITY CLASSIFICATION ---")
    report.append("  Priority 1: Fix immediately (build-breaking errors)")
    report.append("  Priority 2: Fix this sprint (security issues)")
    report.append("  Priority 3: Fix when possible (recurring warnings)")
    report.append("  Priority 4: Ignore/fix opportunistically (one-time)")

    output = "\n".join(report)
    print(output)

    with open("history-report.txt", "w") as f:
        f.write(output)
    print("\nSaved to history-report.txt")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--save":
        build_id = int(sys.argv[2])
        report_path = sys.argv[3]
        result = None
        rest = sys.argv[4:]
        while rest:
            flag, rest = rest[0], rest[1:]
            if flag == "--result" and rest:
                result, rest = rest[0], rest[1:]
        save_build(build_id, report_path, result=result)
    elif len(sys.argv) > 1 and sys.argv[1] == "--seed":
        seed_from_history()
    else:
        analyze_all()