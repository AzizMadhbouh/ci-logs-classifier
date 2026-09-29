"""Feed ALL Jenkins builds (past + present) into Postgres for Grafana.

Pulls each build's metadata (build.xml) and archived output (build-output.log,
falling back to the console log) from the `jenkins` container running in WSL2,
parses per-tool issues with severity, and upserts into the `builds` and
`build_issues` tables.

Usage:
    python feed_jenkins_builds.py                     # all builds
    python feed_jenkins_builds.py --since 30          # only build >= 30
    python feed_jenkins_builds.py --clear             # delete rows first
"""
import argparse
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)

from analyze_history import ensure_schema, get_db_conn
from ml.severity_llm import classify_severity
from ml.rootcause_model import extract_evidence_lines
from ml.category_model import predict_category

WSL_DISTRO = "Ubuntu"
CONTAINER = "jenkins"
JOB_BUILDS = "/var/jenkins_home/jobs/sales-analyzer/builds"

SEVERITY_POLICY = {
    "pylint": {"F": "Critical", "E": "High", "W": "Medium", "C": "Low", "R": "Medium"},
    "flake8": {"E": "Medium", "W": "Low", "F": "Medium", "C": "Low"},
}
ERROR_LEVELS = {"High", "Critical"}

FLAKE8_RE = re.compile(r"^([^:]+):\d+:\d+:\s+([A-Z]\d{3})\s+(.*)$")
PYLINT_RE = re.compile(r"^([^:]+):\d+:\d+:\s+([A-Z]\d{4}):\s+(.*)\)\s*$")
MYPY_RE = re.compile(r"^([^:]+):\d+:\s+(error|note|warning):\s+(.*)$")
PYTEST_FAIL_RE = re.compile(r"^(tests?/[^\s:]+(?:::[^ ]+)+)\s+FAILED\s+\[")
BLACK_RE = re.compile(r"^would reformat\s+(.+)$")
BANDIT_ISSUE_RE = re.compile(r"^>> Issue:\s+\[([A-Z]\d+:\w+)\]\s*(.*)$")
BANDIT_SEVERITY_RE = re.compile(r"^Severity:\s*(Low|Medium|High|Undefined)")
BANDIT_LOCATION_RE = re.compile(r"^Location:\s*(.+)$")

BANDIT_CONTINUATION = ("Severity:", "Confidence:", "CWE:", "More Info:", "Location:")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def wsl_run(*args):
    r = subprocess.run(
        ["wsl", "-d", WSL_DISTRO, "--", "docker", "exec", CONTAINER, *args],
        capture_output=True,
    )
    return r.returncode, r.stdout


def list_builds():
    rc, out = wsl_run("bash", "-c", f"ls -1 {JOB_BUILDS}")
    if rc != 0:
        print(f"FATAL: cannot list builds: {out.decode()!r}", file=sys.stderr)
        sys.exit(1)
    nums = sorted(int(n) for n in out.decode().split() if n.isdigit())
    return nums


def fetch_build(n):
    """Return (meta_text, log_text, artifact_files_dict)."""
    base = f"{JOB_BUILDS}/{n}"
    script = (
        f"cat {base}/build.xml; echo '=====LOG====='; "
        f"cat {base}/archive/build-output.log 2>/dev/null || cat {base}/log 2>/dev/null; "
        f"echo '=====ARTIFACTS====='; "
        f"ls -1 {base}/archive/ 2>/dev/null || true"
    )
    rc, out = wsl_run("sh", "-c", script)
    if rc != 0:
        return None, None, {}
    text = out.decode("utf-8", errors="replace")
    if "=====LOG=====" not in text:
        return text, "", {}
    meta, _, rest = text.partition("=====LOG=====")
    log, _, artifacts_list = rest.partition("=====ARTIFACTS=====")
    artifacts = {}
    for fname in artifacts_list.strip().splitlines():
        fname = fname.strip()
        if fname:
            artifact_script = f"cat {base}/archive/{fname} 2>/dev/null"
            rc, out = wsl_run("sh", "-c", artifact_script)
            if rc == 0:
                artifacts[fname] = out.decode("utf-8", errors="replace")
    return meta, log, artifacts


def parse_meta(meta):
    m_ts = re.search(r"<startTime>(\d+)</startTime>", meta)
    m_res = re.search(r"<result>([A-Z_]+)</result>", meta)
    ts = datetime.fromtimestamp(int(m_ts.group(1)) / 1000, tz=timezone.utc) if m_ts else datetime.now(tz=timezone.utc)
    result = m_res.group(1) if m_res else None
    return ts, result


FLAKE8_RE = re.compile(r"^([^:]+):\d+:\d+:\s+([A-Z]\d{3})\s+(.*)$")
PYLINT_RE = re.compile(r"^([^:]+):\d+:\d+:\s+([A-Z]\d{4}):\s+(.*)\)\s*$")
MYPY_RE = re.compile(r"^([^:]+):\d+:\s+(error|note|warning):\s+(.*)$")
PYTEST_FAIL_RE = re.compile(r"^(tests?/[^\s:]+(?:::[^ ]+)+)\s+FAILED\s+\[")
BLACK_RE = re.compile(r"^would reformat\s+(.+)$")
BANDIT_ISSUE_RE = re.compile(r"^>> Issue:\s+\[([A-Z]\d+:\w+)\]\s*(.*)$")
BANDIT_SEVERITY_RE = re.compile(r"^Severity:\s*(Low|Medium|High|Undefined)")
BANDIT_LOCATION_RE = re.compile(r"^Location:\s*(.+)$")

BANDIT_CONTINUATION = ("Severity:", "Confidence:", "CWE:", "More Info:", "Location:")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


BANDIT_CONTINUATION = ("Severity:", "Confidence:", "CWE:", "More Info:", "Location:")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def classify_line(line, pending_bandit):
    """Returns (issue dict or None, updated pending_bandit)."""
    issue = None

    if line.startswith(">>"):
        m = BANDIT_ISSUE_RE.match(line)
        if m:
            return None, {"code": m.group(1), "msg": m.group(2).strip(), "sev": None, "loc": None}
        return None, pending_bandit

    if pending_bandit is not None:
        m = BANDIT_SEVERITY_RE.match(line)
        if m:
            pending_bandit["sev"] = "Medium" if m.group(1) == "Undefined" else m.group(1)
            return None, pending_bandit
        m = BANDIT_LOCATION_RE.match(line)
        if m:
            pending_bandit["loc"] = m.group(1).strip()
            sev = pending_bandit["sev"] or "Medium"
            code = pending_bandit["code"]
            line_txt = f"{code}: {pending_bandit['msg']}  ->  {pending_bandit['loc']}"
            issue = {"severity": sev, "category": "bandit", "line": line_txt, "is_error": sev in ERROR_LEVELS}
            return issue, None
        if line.strip() and not any(line.startswith(p) for p in BANDIT_CONTINUATION):
            return None, None
        return None, pending_bandit

    if line.startswith("would reformat"):
        m = BLACK_RE.match(line)
        if m:
            return {"severity": "Medium", "category": "black", "line": m.group(1), "is_error": False}, None

    m = MYPY_RE.match(line)
    if m:
        kind = m.group(2)
        if kind == "error":
            sev, err = "High", True
        else:
            sev, err = "Low", False
        return {"severity": sev, "category": "mypy", "line": line.strip(), "is_error": err}, None

    m = PYLINT_RE.match(line)
    if m:
        sev = SEVERITY_POLICY["pylint"].get(m.group(2)[0], "Medium")
        return {"severity": sev, "category": "pylint", "line": line.strip(), "is_error": sev in ERROR_LEVELS}, None

    m = FLAKE8_RE.match(line)
    if m:
        sev = SEVERITY_POLICY["flake8"].get(m.group(2)[0], "Medium")
        return {"severity": sev, "category": "flake8", "line": line.strip(), "is_error": sev in ERROR_LEVELS}, None

    m = PYTEST_FAIL_RE.match(line)
    if m:
        return {"severity": "High", "category": "test", "line": line.strip(), "is_error": True}, None

    return None, pending_bandit


def parse_sarif_flake8(sarif_text):
    """Parse flake8 SARIF output into issue dicts."""
    issues = []
    try:
        data = json.loads(sarif_text)
        for run in data.get("runs", []):
            tool = run.get("tool", {}).get("driver", {})
            rules = {r.get("id"): r for r in tool.get("rules", [])}
            for result in run.get("results", []):
                rule_id = result.get("ruleId", "")
                level = result.get("level", "warning")
                msg = result.get("message", {}).get("text", "")
                locations = result.get("locations", [])
                loc_str = ""
                if locations:
                    phys = locations[0].get("physicalLocation", {})
                    region = phys.get("region", {})
                    start_line = region.get("startLine", "?")
                    artifact = phys.get("artifactLocation", {}).get("uri", "")
                    loc_str = f"{artifact}:{start_line}"
                sev = "High" if level == "error" else "Medium"
                is_err = level == "error"
                line = f"{loc_str}: {rule_id} {msg}" if loc_str else f"{rule_id} {msg}"
                issues.append({"severity": sev, "category": "flake8", "line": line, "is_error": is_err})
    except Exception:
        pass
    return issues


def parse_sarif_bandit(sarif_text):
    """Parse bandit SARIF output into issue dicts."""
    issues = []
    try:
        data = json.loads(sarif_text)
        for run in data.get("runs", []):
            for result in run.get("results", []):
                level = result.get("level", "warning")
                msg = result.get("message", {}).get("text", "")
                rule_id = result.get("ruleId", "")
                locations = result.get("locations", [])
                loc_str = ""
                if locations:
                    phys = locations[0].get("physicalLocation", {})
                    region = phys.get("region", {})
                    start_line = region.get("startLine", "?")
                    artifact = phys.get("artifactLocation", {}).get("uri", "")
                    loc_str = f"{artifact}:{start_line}"
                sev = "High" if level == "error" else "Medium"
                is_err = level == "error"
                line = f"{loc_str}: {rule_id} {msg}" if loc_str else f"{rule_id} {msg}"
                issues.append({"severity": sev, "category": "bandit", "line": line, "is_error": is_err})
    except Exception:
        pass
    return issues


def parse_pylint_json(json_text):
    """Parse pylint JSON output into issue dicts."""
    issues = []
    try:
        data = json.loads(json_text)
        for item in data:
            msg_id = item.get("message-id", "")
            msg = item.get("message", "")
            path = item.get("path", "")
            line_no = item.get("line", "?")
            col = item.get("column", "?")
            typ = item.get("type", "warning")
            sev_map = {"error": "High", "fatal": "Critical", "warning": "Medium", "refactor": "Low", "convention": "Low", "info": "Low"}
            sev = sev_map.get(typ, "Medium")
            is_err = typ in ("error", "fatal")
            line = f"{path}:{line_no}:{col}: {msg_id} {msg}"
            issues.append({"severity": sev, "category": "pylint", "line": line, "is_error": is_err})
    except Exception:
        pass
    return issues


def parse_junit_xml(xml_text):
    """Parse JUnit XML (pytest) into issue dicts."""
    issues = []
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml_text)
        for testsuite in root.iter("testsuite"):
            for testcase in testsuite.iter("testcase"):
                failure = testcase.find("failure")
                error = testcase.find("error")
                if failure is not None or error is not None:
                    name = testcase.get("name", "")
                    classname = testcase.get("classname", "")
                    msg = (failure.text if failure is not None else error.text) or ""
                    line = f"{classname}.{name} {msg}"
                    issues.append({"severity": "High", "category": "test", "line": line, "is_error": True})
    except Exception:
        pass
    return issues


def parse_black_log(log_text):
    """Parse black 'would reformat' lines from log (no structured output)."""
    issues = []
    for raw in log_text.splitlines():
        line = ANSI_RE.sub("", raw).rstrip()
        if line.startswith("would reformat"):
            m = BLACK_RE.match(line)
            if m:
                issues.append({"severity": "Medium", "category": "black", "line": m.group(1), "is_error": False})
    return issues


def parse_artifacts(artifacts, log_text):
    """Parse all artifact files and return combined issues."""
    all_issues = []
    has_structured = False
    # flake8 SARIF
    if "flake8.sarif" in artifacts:
        all_issues.extend(parse_sarif_flake8(artifacts["flake8.sarif"]))
        has_structured = True
    # bandit SARIF
    if "bandit.sarif" in artifacts:
        all_issues.extend(parse_sarif_bandit(artifacts["bandit.sarif"]))
        has_structured = True
    # pylint JSON
    if "pylint.json" in artifacts:
        all_issues.extend(parse_pylint_json(artifacts["pylint.json"]))
        has_structured = True
    # JUnit XML (pytest)
    if "report.xml" in artifacts:
        all_issues.extend(parse_junit_xml(artifacts["report.xml"]))
        has_structured = True
    # black (from log only - no structured output yet)
    all_issues.extend(parse_black_log(log_text))
    # mypy (from log only - no structured output yet)
    for raw in log_text.splitlines():
        line = ANSI_RE.sub("", raw).rstrip()
        m = MYPY_RE.match(line)
        if m:
            kind = m.group(2)
            sev, err = ("High", True) if kind == "error" else ("Low", False)
            all_issues.append({"severity": sev, "category": "mypy", "line": line.strip(), "is_error": err})
    return all_issues


def ingest_build(cur, n, require_complete=True, llm=True):
    """Fetch one build from Jenkins and upsert it into Postgres. Returns True if data was written."""
    meta, log, artifacts = fetch_build(n)
    if meta is None or not log.strip():
        return False
    ts, result = parse_meta(meta)
    if require_complete and (result is None or "<completed>true" not in meta):
        return False
    issues = parse_artifacts(artifacts, log)
    errors = [i for i in issues if i["is_error"]]
    warnings = [i for i in issues if not i["is_error"]]
    is_clean = result == "SUCCESS" and len(errors) == 0
    # Use issue lines as evidence
    evidence = "\n".join(i["line"] for i in issues) if issues else log[-10000:]
    if is_clean:
        category = "clean"
    elif issues:
        category, _, _ = predict_category(evidence)
    else:
        category, _, _ = predict_category(log[-10000:])
    cur.execute("DELETE FROM build_issues WHERE build_id = %s", (n,))
    cur.execute(
        "INSERT INTO builds (build_id, timestamp, error_count, warning_count, trend, result) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (build_id) DO UPDATE SET "
        "timestamp = EXCLUDED.timestamp, error_count = EXCLUDED.error_count, "
        "warning_count = EXCLUDED.warning_count, result = EXCLUDED.result",
        (n, ts, len(errors), len(warnings), "", result),
    )
    cur.execute(
        "UPDATE builds SET category = %s WHERE build_id = %s",
        (category, n),
    )
    for e in errors:
        cur.execute(
            "INSERT INTO build_issues (build_id, timestamp, severity, category, line, is_error) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (n, ts, e["severity"], e["category"], e["line"], True),
        )
    for w in warnings:
        cur.execute(
            "INSERT INTO build_issues (build_id, timestamp, severity, category, line, is_error) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (n, ts, w["severity"], w["category"], w["line"], False),
        )
    if llm:
        cur.execute("SELECT llm_severity FROM builds WHERE build_id = %s", (n,))
        if cur.fetchone()[0] is None:
            if is_clean:
                sev, source, reason = "Low", "policy", "Clean build: 0 errors, all tests pass"
            else:
                sev, source, reason = classify_severity(
                    category, evidence, fallback_policy=False
                )
            if sev is not None:
                cur.execute(
                    "UPDATE builds SET llm_severity = %s, severity_source = %s, severity_reason = %s "
                    "WHERE build_id = %s",
                    (sev, source, reason, n),
                )
    return True


def max_build_in_db(cur):
    cur.execute("SELECT COALESCE(MAX(build_id), 0) FROM builds")
    return cur.fetchone()[0]


def ingest_many(cur, builds, verbose=True, llm=True):
    written = 0
    for n in builds:
        ok = ingest_build(cur, n, llm=llm)
        if verbose and ok:
            print(f"  {n}: ingested", flush=True)
        cur.connection.commit()
        written += int(ok)
    return written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=int, default=0)
    ap.add_argument("--clear", action="store_true")
    ap.add_argument("--no-llm", action="store_true", help="skip LLM severity (rule-based only)")
    args = ap.parse_args()

    builds = [n for n in list_builds() if n >= args.since]
    print(f"Found {len(builds)} builds ({builds[0]}..{builds[-1]})")

    ensure_schema()
    conn = get_db_conn()
    cur = conn.cursor()

    if args.clear:
        cur.execute("DELETE FROM build_issues")
        cur.execute("DELETE FROM builds")
        conn.commit()
        print("Cleared existing rows.")

    written = ingest_many(cur, builds, llm=not args.no_llm)
    conn.commit()
    conn.close()
    print(f"Done. {written}/{len(builds)} builds ingested.")


if __name__ == "__main__":
    main()