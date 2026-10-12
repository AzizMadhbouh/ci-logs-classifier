# CI Logs Classifier

End-to-end CI failure intelligence: ingest Jenkins builds, classify each failure with a
local ModernBERT model, score severity (LLM + rules), store everything in Postgres, and
visualize it in Grafana. Includes production guardrails (tool-output pre-filters, confidence
rejection, LLM adjudication) and structured prediction logging for drift monitoring.

> **Status:** tests green — **84 passed, 1 skipped** (`python -m pytest tests/ -q`,
> verified 2026-10-12). Live pipeline snapshot: **46 builds (26 FAILURE / 20 SUCCESS),
> 107 issues**, 2026-08-21 → 2026-09-18.

---

## Architecture

![Architecture](docs/architecture.svg)

Per build: **collect → parse → classify → score → store → visualize**.
`builds.severity_source` records `llm` vs `policy`; every prediction logs
label/confidence/source/rejected for drift monitoring.

## Results (measured, not claimed)

### Category model — 10-class base (recorded training outputs, epoch 3.0)

`eval_accuracy` **0.7709**, `eval_f1` **0.7714**, support **14,320**:

| class | precision | recall | f1 | support |
|---|---|---|---|---|
| auth_permission_error | 0.79 | 0.83 | 0.81 | 1489 |
| ci_config_git | 0.92 | 0.85 | 0.88 | 1499 |
| containers_docker | 0.82 | 0.84 | 0.83 | 1426 |
| database_error | 0.73 | 0.78 | 0.76 | 1492 |
| network_api_error | 0.72 | 0.73 | 0.72 | 1463 |
| out_of_memory | 0.81 | 0.74 | 0.77 | 1496 |
| runtime_error | 0.68 | 0.72 | 0.70 | 1493 |
| syntax_error | 0.73 | 0.69 | 0.71 | 1491 |
| test_failure | 0.73 | 0.81 | 0.77 | 1414 |
| timeout_error | 0.82 | 0.70 | 0.76 | 1057 |
| **macro avg** | **0.78** | **0.77** | **0.77** | **14320** |

### Category model — 32-class extension

Fine-tuned on 28,471 real CI logs + 843 Veloren logs (`ml/finetune_category.py`,
`ml/category_extension_training.ipynb`): original 10 head weights preserved, 22 new
classes Xavier-initialized, two-phase train (head warmup → full fine-tune).
**E2E-verified** against the real model (wire-in, pre-filter/reject/LLM paths, logging,
feed compatibility) and **26/26 `test_category_model.py` tests pass**.
Full 5-fold cross-validation of the 32-class head (`ml/cross_val_eval.py`) is
implemented but not yet run — see Limitations.

### Live pipeline snapshot (Postgres, queried 2026-10-12)

| metric | value |
|---|---|
| builds | 46 (26 FAILURE / 20 SUCCESS) |
| build_issues | 107 |
| window | 2026-08-21 → 2026-09-18 |
| top categories | clean 18, Test 11, syntax_error 9, Repository 4, CI Orchestration 3, unknown 1 |

Note the `unknown` row: the rejection path is live in production data, not just tests.

## Production guardrails

- **Tool-output pre-filter** — 7 anchored regexes (ruff/flake8/pylint/mypy/bandit/eslint/tsc/shellcheck/trivy/gitleaks/pytest) short-circuit obvious tool lines before the model.
- **Confidence rejection** — `CATEGORY_REJECT_THRESHOLD` (default 0.6); below-threshold predictions become `unknown` instead of a wrong label.
- **LLM adjudication** — top-k Gemini re-rank when a key is configured; raises cleanly otherwise so callers fall back to `unknown`.
- **No silent failures** — severity always resolves (`llm` → `policy`), and the source is stored per build.

## Screenshots

Drop two PNGs in `docs/screenshots/` and they render here:

- `docs/screenshots/severity-dashboard.png` — *CI Build Severity & Health*
- `docs/screenshots/issues-dashboard.png` — *CI Build Issues Deep Dive*

```text
docs/screenshots/severity-dashboard.png
docs/screenshots/issues-dashboard.png
```

## Getting started

Prerequisites: Windows + WSL2, Docker Desktop, Python 3.12+.

```powershell
pip install -r requirements.txt
docker compose up -d postgres grafana        # Grafana → http://localhost:3001
alembic upgrade head                         # fresh DB only
python feed_jenkins_builds.py --since 1      # runs on host (needs `wsl` binary)
```

| file | purpose |
|---|---|
| `feed_jenkins_builds.py` | bulk ingestion of builds into Postgres |
| `watch_builds.py` | poll loop (`--interval 30`); compose `--profile watch` |
| `ml/category_model.py` | 32-class classifier + guardrail pipeline |
| `ml/finetune_category.py`, `ml/category_extension_training.ipynb` | extension training |
| `ml/cross_val_eval.py` | stratified k-fold evaluation |
| `ml/severity_llm.py` | Gemini severity + category adjudication |
| `grafana/make_dashboards.py` | regenerate dashboard JSON |
| `migrations/` | Alembic revisions (`builds`, `build_issues`) |
| `tests/` | pytest suite (parsers, LLM, analyzer, reporter, loader, category model) |

`.env` (gitignored) keys: `DB_*` / `POSTGRES_*`, `GEMINI_API_KEY`,
`GF_ADMIN_USER` / `GF_ADMIN_PASSWORD`. Model weights (`model.safetensors`,
`category_model_finetuned/`, `*.zip`, `FineTuning*.ipynb`) are gitignored —
fetch the model from Hugging Face instead of committing binaries.

## Limitations (honest)

- Training labels are **heuristic/regex-assigned, not human-verified**; the 10 original
  classes have zero test coverage.
- 32-class head has e2e + unit verification but **no full cross-validation numbers yet**.
- Gemini free tier (~20 req/day/model) → expect `policy` fallbacks on large back-fills.
- Ingestion is coupled to this Windows/WSL2 host (`wsl` binary); no Postgres backups;
  dependencies unpinned (`>=` floors, `grafana:latest`).

## Security notes

- `.env` is gitignored **and** was never committed; full history scanned 2026-10-12 —
  no API keys, tokens, private keys, or hardcoded passwords found (only variable-name
  references and weak compose *defaults*, which fail safe only with `.env` present).
- Heavy artifacts and personal files are gitignored; verify with `git ls-files`.
