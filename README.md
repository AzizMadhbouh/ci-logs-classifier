# CI Logs Classifier

End-to-end CI failure intelligence for Jenkins builds: ingest build metadata, logs and
archived lint/test artifacts, classify each failure with a local ModernBERT model, score
its severity with a Gemini LLM, store everything in Postgres and visualize it in Grafana.

> Repository: `AzizMadhbouh/ci-logs-classifier` (formerly `sales-analyzer` — the original
> sales-CSV report code still lives in `src/` and `main.py` and remains covered by CI).

[![CI](https://github.com/AzizMadhbouh/ci-logs-classifier/actions/workflows/ci.yml/badge.svg)](https://github.com/AzizMadhbouh/ci-logs-classifier/actions/workflows/ci.yml)
[![Test](https://github.com/AzizMadhbouh/ci-logs-classifier/actions/workflows/test.yml/badge.svg)](https://github.com/AzizMadhbouh/ci-logs-classifier/actions/workflows/test.yml)

---

## 1. Architecture

```
┌─────────────────────────────── WSL2 / Docker ────────────────────────────────┐
│  jenkins (lts)            postgres-buildhistory (pg16)     grafana (:3001)  │
│  :8080/:50000                   :5432                          :3000→3001  │
│      │                                ▲                            ▲       │
│      │ build.xml, console log,        │ SQL upsert                 │ SQL   │
│      │ archived *.sarif/*.json/log    │                            │       │
└──────┼────────────────────────────────┼────────────────────────────┼───────┘
       │  docker exec (via wsl_run)     │                            │
       ▼                                │                            │
 feed_jenkins_builds.py ────────────────┤                            │
 watch_builds.py (poll loop) ───────────┤                            │
       │                               │                            │
       │ per build:                     │                            │
       │  1. parse issues (SARIF,       │                            │
       │     pylint, JUnit, black,      │                            │
       │     mypy, raw-log fallback)    │                            │
       │  2. category = ModernBERT      │                            │
       │     (ml/category_model.py)     │                            │
       │  3. severity = Gemini LLM      │                            │
       │     (ml/severity_llm.py,       │                            │
       │      policy fallback)          │                            │
       ▼                                │                            │
   builds + build_issues tables ────────┘                            │
                                                                     │
       Grafana dashboards: CI Builds/… ──────────────────────────────┘
```

## 2. What happens for every build

1. **Collect** — `feed_jenkins_builds.py` / `watch_builds.py` run inside the `jenkins`
   container (`docker exec`) to read `build.xml` (metadata), the archived artifacts and
   `build-output.log` (falling back to the console log).
2. **Parse** — per-tool parsers extract issues with error/warning severity:
   * flake8 SARIF, bandit SARIF, pylint JSON, JUnit XML, black log, mypy + raw log
     (`classify_line` fallback). Builds without archives report 0 structured issues.
3. **Classify** — the **category model** predicts one of 10 failure categories from
   *evidence lines* (not the whole log prefix) — see §4.1.
4. **Score** — the **severity LLM** (Gemini) grades overall build severity with a
   short evidence-based reason; if the LLM is unavailable/rate-limited, the rule policy
   in `ml/severity_policy.json` decides. The winner is recorded in `severity_source`.
5. **Store** — one row in `builds` + N rows in `build_issues` (Postgres).
6. **Visualize** — Grafana dashboards on `http://localhost:3001` read Postgres directly.

## 3. Repository layout

| Path | Purpose |
|---|---|
| `feed_jenkins_builds.py` | Bulk ingestion of Jenkins builds into Postgres (entry point) |
| `watch_builds.py` | Near-real-time watcher: polls for new builds and ingests them |
| `build_report.py` | Human-readable report for one build → `build_<N>_report.txt` |
| `analyze_history.py` | Offline/history analysis; also the Jenkins post-action report |
| `predict.py` | Legacy root-cause predictor (HuggingFace model + severity LLM) |
| `ml/category_model.py` | **Category classifier** (ModernBERT, lazy load, local/HF) |
| `ml/severity_llm.py` | **Severity LLM** (Gemini, model rotation, retries, policy fallback) |
| `ml/rootcause_model.py` | Evidence-line extraction + legacy root-cause model loader |
| `ml/severity_policy.json` | Rule-based severity fallback (tracked in git) |
| `ml/*.ipynb`, `train.py` | Training / dataset-building experiments |
| `migrations/` | Alembic migrations (`0001_initial` → `builds`, `build_issues`) |
| `grafana/` | Datasource + dashboard provisioning, `make_dashboards.py` |
| `src/`, `main.py` | Legacy sales-CSV analyzer (data_loader/analyzer/reporter) |
| `tests/` | pytest suite (parsers, severity LLM, analyzer, reporter, loader) |
| `Jenkinsfile` | CI pipeline run by Jenkins on this repo |
| `.github/workflows/` | `test.yml` + `ci.yml` (see §9) |
| `Dockerfile`, `docker-compose.yml` | Image + `feed` / `watcher` / `postgres` services |
| `requirements.txt` | Python dependencies (incl. CPU torch wheels) |
| `.env` | Secrets & endpoints — **gitignored**, never committed |

## 4. ML pipeline

### 4.1 Category classifier (`ml/category_model.py`)

* **Model:** `ferjaboss/ci-logs-classifier` — ModernBERT sequence classifier, 10 labels:

  `auth_permission_error`, `ci_config_git`, `containers_docker`, `database_error`,
  `network_api_error`, `out_of_memory`, `runtime_error`, `syntax_error`,
  `test_failure`, `timeout_error`
* **Input:** evidence lines extracted by `ml/rootcause_model.extract_evidence_lines`
  (ANSI stripped, noise removed — pipeline echoes, ha:// links, `# timeout=` etc., max
  15 lines / 8000 chars), never the raw log prefix.
* **Loading:** lazy; prefers the model files in the project root
  (`config.json`, `model.safetensors`, `tokenizer.json`), otherwise downloads from the
  public HuggingFace repo (requires `HF_TOKEN` for the gated repo, or place the files
  locally). Weights (~598 MB) are **gitignored** and excluded from the Docker image.
* A build is considered *clean* when `result == SUCCESS` and `error_count == 0`.

### 4.2 Severity LLM (`ml/severity_llm.py`)

* **Provider:** Gemini (`.env`: `SEVERITY_LLM_PROVIDER=gemini`, `GEMINI_API_KEY`,
  `GEMINI_MODEL`).
* **Prompt:** job name + evidence lines + parsed issues → severity
  (`critical | high | medium | low | info`) + short reason.
* **Resilience:** rotates through fallback model IDs on HTTP 429, honors RetryInfo
  delays, up to 6 attempts.
* **Fallback:** if the LLM still fails, `ml/severity_policy.json` rules decide; the
  `builds.severity_source` column records `llm` vs `policy`.
* **Quota note:** free tier ≈ 20 requests/day *per model* — large back-fills may fall
  back to the policy for some builds (expected, not an error).

### 4.3 Root cause (legacy)

`predict.py` + `ml/rootcause_model.py` load a HuggingFace root-cause model for
line-level predictions. Still available for experiments; the main pipeline relies on
category + severity instead.

## 5. Database

Postgres database `buildhistory`, managed with Alembic (dialect
`postgresql+psycopg2://`, credentials from `.env`):

```bash
alembic upgrade head        # create schema (fresh DB)
alembic downgrade -1        # roll back one revision
```

* **`builds`** — `build_id` (PK = Jenkins build number), `timestamp`, `result`,
  `error_count`, `warning_count`, `trend`, `category`, `llm_severity`,
  `severity_source`, `severity_reason` (shown as *reason* in dashboards).
  Indexes on `timestamp`, `category`.
* **`build_issues`** — `issue_id` PK, `build_id` FK → `builds` (CASCADE), `timestamp`,
  `severity`, `category`, `line`, `is_error`. Indexes on `build_id`, `severity`.

## 6. Grafana

* URL: `http://localhost:3001` (container maps `3001 → 3000`; admin user/password from
  `.env` → `GF_ADMIN_USER` / `GF_ADMIN_PASSWORD`).
* Provisioning in `grafana/`:
  * `provisioning/datasources/postgres.yml` — default datasource *Build History
    Postgres* (`${DB_HOST}:${DB_PORT}`, `${DB_NAME}`, `${DB_USER}` / `${DB_PASSWORD}`).
  * `provisioning/dashboards/dashboards.yml` → folder **CI Builds**, loads JSON from
    `/etc/grafana/provisioning/dashboards/definitions`.
  * `provisioning/dashboards/ci-dashboards.yml` → `/var/lib/grafana/dashboards`.
* Dashboards: `ci_build_issues` (counts/trends per category & severity) and
  `ci_build_severity` (severity breakdown with reason).
* Regenerate the dashboard JSON after SQL/logic changes:

  ```bash
  python grafana/make_dashboards.py
  ```

## 7. Jenkins pipeline (`Jenkinsfile`)

Runs on the `jenkins` container (WSL2 Docker):

| Stage | What it does |
|---|---|
| Setup | apt python3, venv, `pip install -r requirements.txt` + lint/test tools |
| Analyze | `black --check`, `flake8` (text **and** SARIF), `mypy`, `bandit` (text + SARIF), `pylint` JSON |
| Test | `pytest --cov=src --junitxml=report.xml` |
| Check | Fails the build if `build-output.log` shows tracebacks/FAILED/fatal/bandit issues |
| post | `analyze_history.py --save <N> analysis-report.txt`, archives `*.sarif`, `*.json`, `build-output.log`, `htmlcov/`, `report.xml` |

Those archived artifacts are exactly what the ingestion parsers consume
(`feed_jenkins_builds.py` → `parse_artifacts`).

## 8. Getting started

**Prerequisites:** Windows + WSL2 (Ubuntu), Docker, Python 3.13+, and the long-lived
containers `postgres-buildhistory`, `grafana`, `jenkins` (all already exist in this
environment — just start them):

```powershell
wsl -- docker start postgres-buildhistory grafana jenkins
```

1. **Install dependencies**

   ```powershell
   python -m venv .venv; .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. **Configure `.env`** (gitignored — copy these keys, fill in your own values):

   | Key | Meaning |
   |---|---|
   | `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | Postgres target (compose defaults: `builduser` / `buildpass` / `buildhistory`) |
   | `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | Used by `docker-compose` postgres service |
   | `SEVERITY_LLM_PROVIDER`, `GEMINI_API_KEY`, `GEMINI_MODEL` | Gemini severity LLM |
   | `GF_ADMIN_USER` / `GF_ADMIN_PASSWORD` | Grafana login |

3. **Model files** (only needed to actually *run* the classifier): place
   `config.json`, `model.safetensors`, `tokenizer.json` (plus `tokenizer_config.json`,
   `special_tokens_map.json`) in the project root — they come from the (gated)
   HuggingFace repo `ferjaboss/ci-logs-classifier`. Alternative: export `HF_TOKEN` for
   an account with access and the loader will download them on first use.

4. **Create/migrate the schema** (fresh database only):

   ```powershell
   alembic upgrade head
   ```

5. **Ingest builds** (§10) and open Grafana at `http://localhost:3001`.

## 9. CI/CD (GitHub Actions)

Two workflows trigger on every push to `master`:

| Workflow | Jobs / gates |
|---|---|
| **test.yml** | `black --check`, `flake8` (79 cols), `mypy src/`, `pip-audit` *(non-blocking)*, `bandit -r src/`, `pytest` |
| **ci.yml** | lint-test: `ruff`, `black`, `mypy` (entrypoints), `pytest --cov`, `pip-audit` *(non-blocking)* · `docker build` + import smoke test · `alembic upgrade/downgrade` against a disposable `postgres:16-alpine` · Trufflehog secret scan (verified results, fail on findings) |

Local equivalents (run these before pushing):

```powershell
python -m ruff check .
python -m black --check src/ tests/
python -m flake8 src/ tests/
python -m mypy src/
python -m bandit -r src/
python -m pytest -q          # 58 passed, 1 skipped
```

`ruff.toml` keeps Ruff to pyflakes-style rules, with per-file ignores for research
scripts (`ml/*`, top-level entrypoints).

## 10. Usage

```powershell
# Bulk ingestion
python feed_jenkins_builds.py                 # every build
python feed_jenkins_builds.py --since 30      # build >= 30
python feed_jenkins_builds.py --clear         # wipe rows first, then re-ingest

# Continuous watcher (polls the Jenkins container; Ctrl+C to stop)
python watch_builds.py --interval 60
python watch_builds.py --once                 # single poll cycle

# Reporting
python build_report.py 20                     # → build_20_report.txt
python analyze_history.py                     # history analysis

# Dashboards (after changing SQL/layout)
python grafana/make_dashboards.py

# Legacy root-cause prediction
python predict.py <logfile> [hf_repo_id]
```

## 11. Docker

* `Dockerfile` — slim Python image, `ENTRYPOINT ["python"]`, `.dockerignore` keeps model
  weights, datasets and secrets out of the image.
* `docker-compose.yml` services:

  ```bash
  docker compose --profile ingest run --rm feed    # one-shot ingestion
  docker compose --profile watch up -d watcher     # continuous watcher
  docker compose up postgres                       # ephemeral DB (optional;
  ```
  The production DB/Grafana/Jenkins are the long-lived standalone containers
  (`postgres-buildhistory`, `grafana`, `jenkins`) rather than compose services.
  Compose `feed`/`watcher` pass `.env` through and mount `./logs`.

## 12. Testing

* Framework: pytest (`pytest.ini`), suites in `tests/`:
  * `test_parsers.py` — SARIF/pylint/JUnit/black parsing + evidence extraction
  * `test_severity_llm.py` — Gemini prompting, retry/backoff, policy fallback
  * `test_analyzer.py`, `test_data_loader.py`, `test_reporter.py` — legacy `src/` core
* Coverage reported in CI via `pytest --cov=src`.

## 13. Security & operational notes

* **`.env` is gitignored** — never commit keys. The Trufflehog CI job fails the build if
  tokens (GitHub `ghp_…`, Google `AIza…`, generic key patterns) ever land in history.
* **Gated model repo** — anonymous downloads of `ferjaboss/ci-logs-classifier` return
  401; either keep the local files or provide `HF_TOKEN`.
* **Gemini free tier** — ~20 requests/day/model; rotation + policy fallback make
  outages visible but harmless (`severity_source` tells you what happened).
* **PowerShell quirks** — no `&&` in Windows PowerShell 5.1 (use `;`), pipe output to
  files as UTF-16 by default, and quote paths with spaces.
* **Secrets in Jenkins** — job/SCM configuration must not embed personal access tokens;
  use Jenkins credentials (`credentials('…')`) instead.

## 14. Legacy: sales analyzer

The original project (before the CI pivot) is a small CSV pipeline kept fully tested:

* `main.py` → `src/data_loader.load_csv` / `validate_sales_data` → `src/analyzer.py`
  summaries → `src/reporter.generate_csv_report` / `generate_text_report`
  (sample data in `data/sample_sales.csv`).

It shares the lint/test gates and the Docker image with the CI tooling.
