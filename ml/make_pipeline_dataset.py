import os
import random
import re
import sys
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_TRAIN = os.path.join(ROOT, "cicd-selfhealing-dataset", "train.csv")
OUT_DIR = os.path.join(ROOT, "pipeline-dataset")
OUT_CSV = os.path.join(OUT_DIR, "train.csv")
LOGCHUNKS_URL = "https://zenodo.org/api/records/3632351/files/LogChunks.zip/content"
LOGCHUNKS_ZIP = os.path.join(ROOT, "cicd-selfhealing-dataset", "LogChunks.zip")

SEED = 42
TARGET_NEW = 10000

SEVERITY_PREFIX = {"F": "Critical", "E": "High", "W": "Medium", "C": "Low", "R": "Medium"}
FLAKE8_MAP = {"F": "High", "E": "Medium", "W": "Low", "C": "Medium"}

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(s):
    return ANSI_RE.sub("", s)


# --------------------------------------------------------------------------
# 1. Harvest real failure lines from LogChunks (Travis CI)
# --------------------------------------------------------------------------
def load_logchunks(zip_path):
    with zipfile.ZipFile(zip_path) as z:
        chunks = []
        for name in z.namelist():
            norm = name.replace("\\", "/")
            if "/build-failure-reason/" not in norm or not norm.endswith(".xml"):
                continue
            if norm.startswith("__MACOSX"):
                continue
            root = ET.fromstring(z.read(name))
            for ex in root.findall("Example"):
                chunk = (ex.findtext("Chunk") or "").strip()
                if chunk:
                    chunks.append(chunk)
    return chunks


def harvest_real_lines(chunks):
    """Return first informative line of each chunk, ANSI-stripped, deduped."""
    lines = []
    seen = set()
    for c in chunks:
        for ln in c.split("\n"):
            s = strip_ansi(ln).strip()
            if not s:
                continue
            if s in seen:
                continue
            seen.add(s)
            lines.append(s)
            break
    # drop very long noise lines
    lines = [ln for ln in lines if 10 <= len(ln) <= 600]
    return lines


# --------------------------------------------------------------------------
# 2. Classify a line -> (category, severity, component)
# --------------------------------------------------------------------------
PREFIX_RE = re.compile(r"^\s*(?:[\w./\\-]+):\d+(?::\d+)?:\s*([A-Z])\d{3,4}(?::|\s)", re.IGNORECASE)
PYLINT_SUFFIX = re.compile(r"\([a-z0-9-]+\)\s*$", re.IGNORECASE)
BANDIT_SEV = re.compile(r"Severity:\s*(Low|Medium|High)", re.IGNORECASE)


def classify(line: str):
    l = line.strip().lower()

    # bandit
    if line.lstrip().startswith(">> Issue:") or "Severity:" in line or "hardcoded_password" in line.lower():
        return ("Security", severity_from_bandit(line), "Application")
    # mypy / static type errors
    m = re.search(r"error:\s+(.+?)(?:\s+\[[a-z-]+\])?$", line)
    if m and (re.search(r"\[[a-z-]+\]\s*$", line) or "No module named" not in line) and "::" not in line:
        if "error:" in l and re.search(r"\b(error|error:)\b.*\[[a-z-]+\]", line):
            return ("Lint", "High", "Application")
    # pylint / flake8 / ruff codes
    pm = PREFIX_RE.search(line)
    if pm:
        code = pm.group(1).upper()
        if PYLINT_SUFFIX.search(line):
            return ("Lint", SEVERITY_PREFIX.get(code, "Medium"), "Application")
        return ("Lint", FLAKE8_MAP.get(code, "Medium"), "Application")
    # compiler / build errors
    if re.search(r"error (CS|TS)\d{3,4}|\.c(?:pp)?:\d+:\d+:\s*error|error:[^\n]*\bat\b", line, re.IGNORECASE):
        return ("Build", "High", "Application")
    if re.search(r"make: \*\*\*|rake aborted|npm ERR|webpack.*failed|build failed|exit status 1\b|Could not resolve", line, re.IGNORECASE):
        return ("Build", "High", "Application")
    # pytest test failures
    if re.search(r"FAILED|AssertionError|assert |expected .* to (eq|be|equal)|panicked at|Test suite failed|Failures:|1\) Failed|Failed asserting", line, re.IGNORECASE):
        if "AssertionError" in l or "assert " in l or "panicked" in l:
            return ("Test", "High", "Application")
        return ("Test", "Medium", "Application")
    # dependency issues
    if re.search(r"No module named|ImportError|ModuleNotFoundError|externally-managed-environment|Failed to (install|resolve) (dep|package)|pip install.*(error|failed)", line, re.IGNORECASE):
        if "No module named" in l or "ModuleNotFound" in l:
            return ("Runtime", "High", "Application")
        return ("Dependencies", "High", "Application")
    # generic runtime exceptions
    if re.search(r"\b(TypeError|ValueError|RuntimeError|KeyError|AttributeError|IndexError|NullPointer|RuntimeException|Exception in thread)\b", line):
        return ("Runtime", "High", "Application")
    # network
    if re.search(r"timeout|timed? out|connection (refused|reset|closed)|could not resolve host|dns|tls|x509|network", line, re.IGNORECASE):
        return ("Network", "Medium", "Network")
    # configuration
    if re.search(r"config|environment variable|env var|\.env|not configured|missing.*(key|setting)", line, re.IGNORECASE):
        return ("Configuration", "Medium", "Application")

    return ("Runtime", "Medium", "Application")


def severity_from_bandit(line):
    m = BANDIT_SEV.search(line)
    if m:
        return m.group(1).capitalize()
    return "Low"


# --------------------------------------------------------------------------
# 3. Synthetic templates matching the exact tool formats
# --------------------------------------------------------------------------
SRC_FILES = ["src/analyzer.py", "src/reporter.py", "src/data_loader.py", "src/api.py", "src/config.py", "src/db.py", "src/auth.py", "src/utils.py", "src/transform.py", "src/monitor.py"]
TEST_FILES = ["tests/test_analyzer.py", "tests/test_reporter.py", "tests/test_data_loader.py", "tests/test_api.py", "tests/test_db.py", "tests/test_auth.py", "tests/test_utils.py", "tests/test_transform.py", "tests/test_monitor.py"]
CLASSES = ["TestCalculateAverageOrderValue", "TestGenerateTextReport", "TestLoadData", "TestAPI", "TestDatabase", "TestAuth", "TestTransform", "TestUtils", "TestConfig", "TestMonitor", "TestReports", "TestCase"]
TESTS = ["test_average_basic", "test_average_single_row", "test_empty_frame", "test_report_contains_header", "test_report_contains_summary", "test_report_contains_top_products", "test_load_csv", "test_load_missing_file", "test_api_200", "test_api_401", "test_db_connection", "test_auth_token", "test_transform_clean", "test_transform_null", "test_utils_merge", "test_monitor_alert"]
EXCS = ["AssertionError", "TypeError", "ValueError", "KeyError", "IndexError", "AttributeError", "RuntimeError", "ZeroDivisionError"]
PYLINT_CODES = [("C0114", "Missing module docstring", "missing-module-docstring", "Low"), ("C0116", "Missing function or method docstring", "missing-function-docstring", "Low"), ("C0103", "Invalid variable name", "invalid-name", "Low"), ("C0415", "Import outside toplevel", "import-outside-toplevel", "Low"), ("C0117", "Consider changing", "unnecessary-negation", "Low"), ("W0511", "TODO: Refactor this function", "fixme", "Medium"), ("W0612", "Unused variable", "unused-variable", "Medium"), ("W0611", "Unused import", "unused-import", "Medium"), ("W0108", "Lambda may not be necessary", "unnecessary-lambda", "Medium"), ("E0602", "Undefined variable", "undefined-variable", "High"), ("E0401", "Unable to import", "import-error", "High"), ("R0913", "Too many arguments", "too-many-arguments", "Medium"), ("R0902", "Too many instance attributes", "too-many-instance-attributes", "Medium")]
FLAKE8_CODES = [("F401", "'os' imported but unused", "High"), ("F841", "local variable 'x' is assigned to but never used", "High"), ("F821", "undefined name 'x'", "High"), ("E501", "line too long (88 > 79 characters)", "Medium"), ("E302", "expected 2 blank lines, found 1", "Medium"), ("W291", "trailing whitespace", "Low"), ("E128", "continuation line under-indented for visual indent", "Medium"), ("F811", "redefinition of unused 'x'", "High")]
MYPY_CODES = ["attr-defined", "arg-type", "assignment", "operator", "no-untyped-def", "return-value", "union-attr", "misc"]
MYPY_MSGS = ["Function is missing a type annotation", "Argument 1 has incompatible type", 'Unsupported operand types for + ("float" and "None")', 'Item "None" of "Optional[int]" has no attribute "strip"', "Incompatible return value type", "Cannot assign to a read-only property", "Argument has incompatible type"]
BANDIT_ISSUES = [("B105", "hardcoded_password_string", "Possible hardcoded password: 'admin123'"), ("B105", "hardcoded_password_funcarg", "Possible hardcoded password: 'super_secret_password_123'"), ("B110", "try_except_pass", "Try, Except, Pass detected"), ("B608", "hardcoded_sql_expressions", "Possible SQL injection vector through string-based query construction"), ("B603", "subprocess_without_shell_equals_true", "Possible security issue in subprocess call")]
EXPANDED_MODULES = ["src/analyzer", "src/reporter", "src/data_loader", "src/api", "src/config", "src/db", "src/auth", "src/utils", "src/transform", "src/monitor", "src/main", "src/cli", "src/ingest", "src/export", "src/cache", "src/queue"]
PACKAGES = ["pytest", "pandas", "numpy", "scikit-learn", "flask", "psycopg2", "requests", "fastapi", "black", "mypy", "ruff"]
HOSTS = ["127.0.0.1", "192.168.1.15", "db.internal", "api.example.com", "pypi.org", "registry.npmjs.org"]
LOCS = [(f, str(l)) for f in EXPANDED_MODULES for l in (1, 3, 7, 12, 17, 21, 28, 34, 41, 47, 53, 66, 72, 88, 94, 113, 121, 156, 189, 210)]
VERSIONS = ["1.0.0", "1.2.3", "2.1.0", "0.9.2", "3.0.1", "1.13.0"]
PASSWORDS = ["'admin123'", "'super_secret_password_123'", "'password123'", "'letmein'", "'root'", "'secret2024'", "'L3tsGo!'", "'toor'", "'hunter2'", "'p@ssw0rd'"]


def build_err(rng):
    style = rng.randint(1, 6)
    if style == 1:
        return f"make: *** [{rng.choice(['build', 'test', 'lint', 'install', 'deploy'])}] Error {rng.randint(1, 2)}", "Build", "High", "Application"
    if style == 2:
        return f"npm ERR! code {rng.choice(['ELIFECYCLE', 'ENOENT', 'ERR_REQUIRE_ESM', 'ERESOLVE'])}", "Build", "High", "Application"
    if style == 3:
        return f"npm ERR! Failed at the {rng.choice(PACKAGES)}@{rng.choice(VERSIONS)} install script", "Build", "High", "Application"
    if style == 4:
        return f"error TS{rng.randint(1000, 2600)}: Argument of type '{rng.choice(['string', 'number', 'boolean'])}' is not assignable to parameter of type '{rng.choice(['number', 'string'])}'", "Build", "High", "Application"
    if style == 5:
        return f"ERROR: Could not resolve module '{rng.choice(['./constants', './utils', 'react', 'lodash'])}' in {rng.choice(EXPANDED_MODULES)}.py", "Build", "High", "Application"
    return f"error: build step {rng.choice(['compile', 'bundle', 'docker build', 'pip wheel'])} failed with exit code {rng.randint(1, 3)}", "Build", "High", "Application"


def dep_err(rng):
    style = rng.randint(1, 8)
    pkg = rng.choice(PACKAGES)
    ver = rng.choice(VERSIONS)
    rid = rng.randint(10000, 99999)
    if style == 1:
        return f"error: externally-managed-environment: {pkg} is not installable via pip in this environment (python{rng.choice(['3.10', '3.11', '3.12'])})", "Dependencies", "High", "Application"
    if style == 2:
        return f"ERROR: Failed to install package '{pkg}@{ver}'", "Dependencies", "High", "Application"
    if style == 3:
        return f"Could not find a version that satisfies the requirement {pkg}>={rng.choice(['8.0', '3.1', '2.5', '1.9', '0.4'])} (from versions: {rng.choice(['0.1', '0.7.3', '1.0', '1.8.9'])}: none match)", "Dependencies", "High", "Application"
    if style == 4:
        return f"No matching distribution found for {pkg}=={ver}", "Dependencies", "High", "Application"
    if style == 5:
        return f"WARNING: Ignoring invalid distribution -{pkg} ({rng.choice(['.egg-info', '.dist-info'])})", "Dependencies", "Medium", "Application"
    if style == 6:
        return f"ERROR: Could not install packages due to an EnvironmentError: [Errno {rng.choice([13, 28])}] {rng.choice(['Permission denied', 'No space left on device'])}: '{pkg}-{ver}/'", "Dependencies", "High", "Application"
    if style == 7:
        return f"[{rid}] Installing {pkg} failed: subprocess exited with error code {rng.choice([1, 2])}", "Dependencies", "High", "Application"
    return f"Uninstalling {pkg}-{ver}: failed to remove {rng.choice(['package data', 'EGG-INFO', 'direct-url.json'])}", "Dependencies", "Medium", "Application"


def conf_err(rng):
    style = rng.randint(1, 6)
    f, l = rng.choice(LOCS)
    if style == 1:
        return f"ERROR: environment variable {rng.choice(['DATABASE_URL', 'API_TOKEN', 'APP_SECRET', 'KAFKA_BROKERS', 'SPARK_HOME', 'REDIS_URL', 'SLACK_WEBHOOK', 'S3_BUCKET', 'GRAFANA_URL', 'JENKINS_URL'])} is not configured", "Configuration", "Medium", "Application"
    if style == 2:
        return f"ConfigError: missing key '{rng.choice(['api_token', 'db_port', 'timeout_seconds', 'log_level', 'retries', 'bucket', 'region', 'endpoint'])}' in config.json", "Configuration", "Medium", "Application"
    if style == 3:
        return "error: .env file not found in " + rng.choice(EXPANDED_MODULES) + "/", "Configuration", "Medium", "Application"
    if style == 4:
        return f"Invalid configuration: '{rng.choice(['thread_pool_size', 'buffer_mb', 'max_connections', 'batch_size', 'poll_interval'])}' must be a positive integer, got {rng.choice(['null', '-1', '0', '\"auto\"', 'abc'])}", "Configuration", "Medium", "Application"
    if style == 5:
        return f"{f}.py:{l}: error: no value found for required setting '{rng.choice(['api_token', 'db_url', 'tls_cert', 'aws_creds', 'kafka_bootstrap'])}'", "Configuration", "Medium", "Application"
    return f"ERROR: unknown configuration section '{rng.choice(['[app.settings]', '[deploy]', '[metrics]', '[webhook]'])}' - skipping", "Configuration", "Low", "Application"


def net_err(rng):
    style = rng.randint(1, 9)
    host = rng.choice(HOSTS)
    port = rng.choice([5432, 8080, 443, 5000, 6379, 9092, 3306])
    rid = rng.randint(10000, 99999)
    if style == 1:
        return f"timeout waiting for response after {rng.choice([10, 30, 60, 120])}s (request #{rid})", "Network", "Medium", "Network"
    if style == 2:
        return f"ConnectionError: Connection refused to {host}:{port} while connecting to {host} (request #{rid})", "Network", "Medium", "Network"
    if style == 3:
        return "ERROR: Could not resolve host: {host} (request #{rid})".format(host=host, rid=rid), "Network", "Medium", "Network"
    if style == 4:
        return "Network is unreachable: No route to host (request #{rid})".format(rid=rid), "Network", "Medium", "Network"
    if style == 5:
        return "socket.gaierror: [Errno -5] No address associated with hostname (request #{rid})".format(rid=rid), "Network", "Medium", "Network"
    if style == 6:
        return f"Read timeout after {rng.choice([5, 15, 45])}s while connecting to {host}:{port} (request #{rid})", "Network", "Medium", "Network"
    if style == 7:
        return f"curl: ({rng.choice([6, 7, 28])}) Could not {rng.choice(['resolve hostname', 'connect to host'])}; Connection {rng.choice(['timed out', 'refused'])}, is {host} up? (request #{rid})", "Network", "High", "Network"
    if style == 8:
        return f"failed to send request to {host}:{port}: no response before deadline (request #{rid})", "Network", "High", "Network"
    return f"agent lost connectivity to {host}:{port}, retrying with backoff (attempt {rng.randint(2, 9)}, request #{rid})", "Network", "Medium", "Network"


def api_err(rng):
    style = rng.randint(1, 3)
    if style == 1:
        return f"HTTP {rng.choice([500, 502, 503])}: Internal Server Error on {rng.choice(['POST /api/orders', 'GET /api/reports', 'PUT /api/users/1', 'DELETE /api/items/42', 'GET /api/inventory', 'POST /api/auth/refresh'])}", "API", "High", "Application"
    if style == 2:
        return f"requests.exceptions.HTTPError: {rng.choice([401, 403, 404, 409])} Client Error for url: {rng.choice(['/api/report', '/api/order/5', '/api/auth/login', '/api/status', '/api/sync'])}", "API", "High", "Application"
    return "ResponseError: API returned empty payload for request id={id}".format(id=rng.randint(1000, 9999)), "API", "Medium", "Application"


def security_row(rng):
    code, name, msg = rng.choice(BANDIT_ISSUES)
    sev = rng.choices(["Low", "Medium"], weights=[70, 30])[0]
    f, l = rng.choice(LOCS)
    style = rng.randint(1, 4)
    if style == 1:
        return f">> Issue: [{code}:{name}] Possible hardcoded password: {rng.choice(PASSWORDS)}", "Security", sev, "Application"
    if style == 2:
        return f">> Issue: [{code}:{name}] {msg} at {f}:{l}", "Security", sev, "Application"
    if style == 3:
        return f"{f}:{l}: Bandit B{code} ({name}): {msg}", "Security", sev, "Application"
    return f">> Issue: [{code}:{name}] Possible hardcoded password found in {f}:{l}: {rng.choice(PASSWORDS)}", "Security", sev, "Application"


SYNTH_TARGETS = {
    "Test": 2000,
    "Lint": 2000,
    "Build": 1200,
    "Security": 1000,
    "Dependencies": 1000,
    "Configuration": 800,
    "Network": 800,
    "API": 700,
}


def component_for(msg):
    m = msg.lower()
    if any(k in m for k in ("docker", "container", "oci", "entrypoint", "pull", "image nginx", "got permission")):
        return "Docker"
    if any(k in m for k in ("postgres", "psql", "sql", "schema", "cursor", "migrate", "table ")):
        return "PostgreSQL"
    if any(k in m for k in ("kubernetes", "kube", "pod ", "kubectl", "helm")):
        return "Kubernetes"
    if any(k in m for k in ("aws", "ec2", "s3", "eks", "lambda", "vpc", "iam", "arn:")):
        return "AWS"
    if any(k in m for k in ("npm", "pnpm", "yarn", "node_modules", "registry.npmjs")):
        return "GitHub"
    if any(k in m for k in ("jenkins", "pipeline", "stage: ", "build #", "agent ", "batch mode")):
        return "Jenkins"
    if any(k in m for k in ("linux", "usr/bin", "/usr/", "permission denied", "no space")):
        return "Linux"
    if any(k in m for k in ("network", "connect", "socket", "http", "resolve host", "route", "curl", "ping", "timeout", "unreachable", "dns")):
        return "Network"
    return "Application"


def synthetic_rows():
    rng = random.Random(SEED + 1)
    rows = []
    seen = set()
    for cat, target in SYNTH_TARGETS.items():
        misses = 0
        while True:
            if cat == "Test":
                f = rng.choice(TEST_FILES)
                c = rng.choice(CLASSES)
                t = rng.choice(TESTS)
                if rng.random() < 0.7:
                    ex = rng.choice(EXCS)
                    row = (f'{f}::{c}::{t} FAILED - {ex}: {rng.choice(["expected 200", "expected True", "expected no rows", "got None instead"])}', cat, "High", "Application")
                else:
                    msg = rng.choice(["expected 200", "expected 5 rows", "expected True, got False"])
                    row = (f"FAILED {f}::{c}::{t} - {msg}", cat, "High", "Application")
            elif cat == "Lint":
                if rng.random() < 0.5:
                    code, msgt, sym, sev = rng.choice(PYLINT_CODES)
                    sym = (sym or "").replace(")", "")
                    suffix = f"({sym})" if sym else ""
                    row = (f"{rng.choice(SRC_FILES)}:{rng.randint(1, 200)}:{rng.randint(0, 20)}: {code}: {msgt} {suffix}".rstrip(), cat, sev, "Application")
                else:
                    code, msgt, sev = rng.choice(FLAKE8_CODES)
                    row = (f"{rng.choice(SRC_FILES)}:{rng.randint(1, 200)}:{rng.randint(0, 20)}: {code} {msgt}".rstrip(), cat, sev, "Application")
            elif cat == "Build":
                row = build_err(rng)
            elif cat == "Dependencies":
                row = dep_err(rng)
            elif cat == "Configuration":
                row = conf_err(rng)
            elif cat == "Network":
                row = net_err(rng)
            elif cat == "API":
                row = api_err(rng)
            elif cat == "Security":
                row = security_row(rng)
            else:
                raise ValueError(cat)

            msg = row[0]
            if msg in seen:
                # near-pool-exhaustion safety: keep unlimited attempts only for
                # high-carinality templates; bail out small pools after many misses
                misses += 1
                if misses > 200000 and cat in ("Network", "Dependencies", "Configuration"):
                    break
                continue
            misses = 0
            seen.add(msg)
            # derive component from message content so Test/Lint/Build aren't always "Application"
            row = (msg, row[1], row[2], component_for(msg))
            rows.append(row)
            if sum(1 for r in rows if r[1] == cat) >= target:
                break
    return rows


# --------------------------------------------------------------------------
# 4. Build merged dataset
# --------------------------------------------------------------------------
def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    if not os.path.exists(LOGCHUNKS_ZIP):
        print("Downloading LogChunks from Zenodo ...")
        urllib.request.urlretrieve(LOGCHUNKS_URL, LOGCHUNKS_ZIP)
    chunks = load_logchunks(LOGCHUNKS_ZIP)
    print(f"LogChunks: {len(chunks)} failure chunks")

    real = harvest_real_lines(chunks)
    print(f"distinct real lines: {len(real)}")

    real_df = pd.DataFrame(
        [{"error_message": line, "category": c, "severity": s, "component": cp, "source": "logchunks"}
         for line in real for c, s, cp in [classify(line)]]
    )
    # keep only the valuable real lines (something we can label with confidence)
    keep = ["Test", "Lint", "Security", "Build", "Runtime", "Network", "Configuration", "API", "Dependencies"]
    real_df = real_df[real_df["category"].isin(keep)].drop_duplicates(subset=["error_message"])
    print(f"classified real lines kept: {len(real_df)} |", dict(real_df["category"].value_counts()))

    n_real = len(real_df)
    syn = pd.DataFrame(
        synthetic_rows(),
        columns=["error_message", "category", "severity", "component"],
    )
    syn["source"] = "synthetic"
    syn = syn.drop_duplicates(subset=["error_message"])

    new = pd.concat([real_df, syn], ignore_index=True)
    new = new[new["error_message"].str.strip() != ""]

    orig = pd.read_csv(SRC_TRAIN, encoding="utf-8-sig")[["error_message", "category", "severity", "component"]]
    orig = orig.dropna()
    orig["source"] = "original"

    merged = pd.concat([orig, new], ignore_index=True)
    merged = merged.drop_duplicates(subset=["error_message"], keep="first")
    print(f"\nmixed rows: {len(merged)} (original {len(orig)}, new {len(new)})")
    print("category counts:\n", merged["category"].value_counts().sort_index().to_string())
    print("\nseverity counts:\n", merged["severity"].value_counts().to_string())
    print("\ncomponent counts:\n", merged["component"].value_counts().to_string())

    merged[["error_message", "category", "severity", "component"]].to_csv(OUT_CSV, index=False)
    print(f"\nwritten: {OUT_CSV}")


if __name__ == "__main__":
    main()