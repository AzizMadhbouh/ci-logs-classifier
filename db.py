"""Shared Postgres helpers (extracted from legacy analyze_history.py).

Only get_db_conn + ensure_schema are kept — the file-based
build_history analysis, trend scoring and text reports were superseded
by feed_jenkins_builds.py + Alembic migrations + Grafana.
"""

import os

import psycopg2
from pathlib import Path


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
