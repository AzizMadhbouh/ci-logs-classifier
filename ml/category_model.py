"""CI log category classifier (local model files or HuggingFace hub).

Loads the extended model from `category_model_finetuned/` when present
(32-class: 10 original + train_real.csv categories), else falls back to the
original 10-class files in the project root, else the HuggingFace hub.

Serving guardrails (production):
  * tool-output pre-filter: ruff/flake8/pylint/mypy/bandit/eslint/tsc/
    shellcheck/trivy/gitleaks/pytest formats are classified by regex before
    the model ever sees them (the model was never trained on these formats).
  * rejection: model outputs below CATEGORY_REJECT_THRESHOLD (default 0.6)
    return "unknown" instead of a confident misclassification; when a Gemini
    key is configured the top-k candidates are adjudicated by the LLM first.
  * every prediction is logged (ml.category_model logger) with label,
    confidence and source for drift monitoring.
"""
import logging
import os
import re
from pathlib import Path

import torch
from safetensors.torch import load_file
from transformers import AutoConfig

# Repo root (this file lives in <root>/ml/).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Public fallback used when the model files are not present locally
# (Docker image, CI, any machine other than the original dev box).
MODEL_REPO = "ferjaboss/ci-logs-classifier"

# Extended model dir (output of ml/category_extension_training.ipynb).
# Override with CATEGORY_MODEL_DIR. Checked before the legacy root files.
EXTENDED_DIR = Path(
    os.environ.get("CATEGORY_MODEL_DIR", PROJECT_ROOT / "category_model_finetuned")
)

# Below this max softmax probability the model abstains ("unknown").
# Override with CATEGORY_REJECT_THRESHOLD.
REJECT_THRESHOLD = float(os.environ.get("CATEGORY_REJECT_THRESHOLD", "0.6"))

# Fixed confidence reported for regex pre-filter hits.
PREFILTER_CONFIDENCE = 0.90

UNKNOWN_LABEL = "unknown"

log = logging.getLogger("ml.category_model")

_category_tokenizer = None
_category_model = None
_category_device = None
_category_source = None  # which files the loaded model came from


def _model_paths():
    """Return (config_source, tokenizer_file, weights_file).

    Prefers the extended model dir, then the legacy files in the project
    root; otherwise downloads them from the public HuggingFace repo into
    the HF cache.
    """
    for base in (EXTENDED_DIR, PROJECT_ROOT):
        weights = base / "model.safetensors"
        tok = base / "tokenizer.json"
        if weights.exists() and tok.exists():
            return str(base), str(tok), str(weights)

    from huggingface_hub import hf_hub_download

    tok_file = hf_hub_download(MODEL_REPO, "tokenizer.json")
    weights_file = hf_hub_download(MODEL_REPO, "model.safetensors")
    return MODEL_REPO, tok_file, weights_file


def get_category_model():
    """Lazy-load tokenizer + model (extended dir, else local files, else hub)."""
    global _category_tokenizer, _category_model, _category_device, _category_source
    if _category_tokenizer is None:
        from tokenizers import Tokenizer
        from transformers import AutoModelForSequenceClassification

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        config_source, tok_file, weights_file = _model_paths()

        # Fast tokenizer from tokenizer.json
        tokenizer = Tokenizer.from_file(tok_file)

        # Architecture from config.json, weights from model.safetensors
        config = AutoConfig.from_pretrained(config_source)
        model = AutoModelForSequenceClassification.from_config(config)
        model.load_state_dict(load_file(weights_file))
        model.to(device).eval()

        _category_tokenizer = tokenizer
        _category_model = model
        _category_device = device
        _category_source = config_source
    return _category_tokenizer, _category_model, _category_device


def model_labels(model=None):
    """All labels of the loaded model as an ordered list."""
    if model is None:
        _, model, _ = get_category_model()
    id2label = model.config.id2label or {}
    return [
        id2label.get(i, id2label.get(str(i), str(i)))
        for i in range(model.config.num_labels)
    ]


def _resolve_label(model, *candidates):
    """First candidate present in the model's label set, else None."""
    labels = set(model_labels(model))
    for cand in candidates:
        if cand in labels:
            return cand
    return None


# --------------------------------------------------------------------------
# Tool-output pre-filter: anchored, high-precision formats only.
# Each entry: (compiled regex, preferred labels in priority order).
# --------------------------------------------------------------------------
_TOOL_PATTERNS = [
    # ruff / flake8 / pylint:  path:line:col: CODE [message] [(symbol)]
    (
        re.compile(
            r"^[\w./\\-]+:\d+:\d+:\s*[A-Z]\d{2,4}\b.*(\([a-z0-9-]+\))?\s*$",
            re.IGNORECASE,
        ),
        ("Lint", "syntax_error"),
    ),
    # mypy:  path:line: error: message [code]
    (
        re.compile(r"^[\w./\\-]+:\d+:\s*(error|note|warning):.*\[[a-z][a-z0-9-]*\]\s*$"),
        ("Lint", "syntax_error", "Build"),
    ),
    # bandit:  >> Issue: [B1xx:...] ...  /  Bandit B1xx (...)
    (
        re.compile(r">> Issue:|Bandit\s+B\d{3}\b|hardcoded[_-]password|B10[5-8]\b|B6\d{2}\b"),
        ("Security", "auth_permission_error"),
    ),
    # eslint / tsc:  error TS1234  /  ERROR in ./file  /  ✖ N problems
    (
        re.compile(r"\berror TS\d+\b|ERROR in \S+|\d+ problems? \(\d+ errors?|✖ \d+ problems?"),
        ("Lint", "Build", "syntax_error"),
    ),
    # shellcheck:  path:line:col: warning: ... [SC1234]
    (
        re.compile(r"\[SC\d{3,4}\]"),
        ("Lint", "syntax_error"),
    ),
    # secret scanners: trivy / gitleaks / trufflehog
    (
        re.compile(r"trivy|gitleaks|trufflehog|leaked secret|PRIVATE KEY|AKIA[0-9A-Z]{16}"),
        ("Security", "auth_permission_error"),
    ),
    # pytest failures:  FAILED path::Test::test  /  AssertionError
    (
        re.compile(r"^FAILED\s+\S+::|AssertionError"),
        ("Test", "test_failure"),
    ),
]


def classify_tool_output(logs, model=None):
    """Regex pre-filter for scanner/linter tool output.

    Returns a label present in the model's label set, else None.
    Scans line-by-line; the majority label wins (ties -> first hit).
    """
    if model is None:
        _, model, _ = get_category_model()
    votes = {}
    order = []
    for raw in (logs or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        for pattern, candidates in _TOOL_PATTERNS:
            if pattern.search(line):
                label = _resolve_label(model, *candidates)
                if label is not None:
                    votes[label] = votes.get(label, 0) + 1
                    if label not in order:
                        order.append(label)
                break  # one vote per line at most
    if not votes:
        return None
    best = max(votes.values())
    tied = [label for label in order if votes[label] == best]
    return tied[0]


def _log_prediction(label, prob, source, num_labels):
    log.info(
        "category prediction",
        extra={
            "label": label,
            "confidence": round(float(prob), 4),
            "source": source,
            "num_labels": num_labels,
            "rejected": label == UNKNOWN_LABEL,
        },
    )


def predict_category(logs, topk=3):
    """Predict category for a whole job log. Returns (label, prob, topk).

    Pipeline: tool pre-filter -> model -> reject-if-unsure ("unknown") ->
    Gemini adjudication of the top-k (only when a key is configured).
    """
    tokenizer, model, device = get_category_model()
    labels = model_labels(model)

    prefiltered = classify_tool_output(logs, model)
    if prefiltered is not None:
        top = [(prefiltered, PREFILTER_CONFIDENCE)]
        _log_prediction(prefiltered, PREFILTER_CONFIDENCE, "prefilter", len(labels))
        return prefiltered, PREFILTER_CONFIDENCE, top

    text = (logs or "")[:20000]
    encoding = tokenizer.encode(text)
    input_ids = torch.tensor([encoding.ids], dtype=torch.long).to(device)
    attention_mask = torch.tensor(
        [encoding.attention_mask], dtype=torch.long
    ).to(device)

    with torch.no_grad():
        logits = model(
            input_ids=input_ids, attention_mask=attention_mask
        ).logits.float()
    probs = torch.softmax(logits, dim=-1)[0]
    k = min(topk, len(labels))
    ids = probs.topk(k).indices.tolist()
    id2label = model.config.id2label or {}
    top = [
        (
            id2label.get(i, id2label.get(str(i), str(i))),
            float(probs[i]),
        )
        for i in ids
    ]
    label, prob = top[0]

    if prob >= REJECT_THRESHOLD:
        _log_prediction(label, prob, "model", len(labels))
        return label, prob, top

    # Unsure: adjudicate top-k with the LLM when available, else "unknown".
    try:
        from ml.severity_llm import adjudicate_category

        adjudicated, _reason = adjudicate_category(
            [lab for lab, _ in top], text
        )
        if adjudicated in labels:
            adj_prob = next(p for lab, p in top if lab == adjudicated)
            _log_prediction(adjudicated, adj_prob, "llm", len(labels))
            return adjudicated, adj_prob, top
    except Exception as e:  # noqa: BLE001 - adjudication must never break serving
        log.warning("category LLM adjudication failed: %s", e)

    unknown_top = [(UNKNOWN_LABEL, prob)] + [
        (lab, p) for lab, p in top if lab != UNKNOWN_LABEL
    ][:k]
    _log_prediction(UNKNOWN_LABEL, prob, "unknown", len(labels))
    return UNKNOWN_LABEL, prob, unknown_top
