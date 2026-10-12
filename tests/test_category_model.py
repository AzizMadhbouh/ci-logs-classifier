# tests/test_category_model.py - guardrails for the category classifier.
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import torch

import ml.category_model as cm

# ---------- fakes (never load the 600MB weights in tests) ----------


class FakeTokenizer:
    def encode(self, text):
        return SimpleNamespace(ids=[1, 2, 3], attention_mask=[1, 1, 1])


class FakeModel:
    def __init__(self, labels, probs):
        assert len(labels) == len(probs)
        self.config = SimpleNamespace(
            id2label={i: label for i, label in enumerate(labels)},
            num_labels=len(labels),
        )
        self._logits = torch.log(torch.tensor([probs], dtype=torch.float32))

    def __call__(self, input_ids=None, attention_mask=None):
        return SimpleNamespace(logits=self._logits)


EXTENDED_LABELS = [
    "auth_permission_error",
    "ci_config_git",
    "Lint",
    "Security",
    "Test",
    "Build",
    "Runtime",
    "Network",
]
OLD_LABELS = [
    "auth_permission_error",
    "ci_config_git",
    "containers_docker",
    "database_error",
    "network_api_error",
    "out_of_memory",
    "runtime_error",
    "syntax_error",
    "test_failure",
    "timeout_error",
]


def _patch_model(monkeypatch, labels, probs):
    monkeypatch.setattr(
        cm,
        "get_category_model",
        lambda: (FakeTokenizer(), FakeModel(labels, probs), torch.device("cpu")),
    )


# ---------- pre-filter ----------


@pytest.mark.parametrize(
    "line,expected_extended,expected_old",
    [
        ("src/analyzer.py:1:1: F401 'os' imported but unused", "Lint", "syntax_error"),
        (
            "src/a.py:2:1: E501 line too long (95 > 79 characters)",
            "Lint",
            "syntax_error",
        ),
        (
            "src/a.py:3:0: W0611: Unused import os (unused-import)",
            "Lint",
            "syntax_error",
        ),
        (
            ">> Issue: [B105:hardcoded_password_string] Possible hardcoded password: 'x'",
            "Security",
            "auth_permission_error",
        ),
        (
            "issue.py:1: Bandit B603 (subprocess_without_shell_equals_true): audit",
            "Security",
            "auth_permission_error",
        ),
        (
            "src/analyzer.py:27: error: Unsupported operand types for +  [operator]",
            "Lint",
            "syntax_error",
        ),
        (
            "src/a.ts:10: error TS2345: Argument of type 'string' is not assignable",
            "Lint",
            "syntax_error",
        ),
        ("src/a.sh:5:10: warning: Use $(...) [SC2006]", "Lint", "syntax_error"),
        (
            "trivy fs --severity HIGH,CRITICAL .: found 3 vulnerabilities",
            "Security",
            "auth_permission_error",
        ),
        (
            "FAILED tests/test_analyzer.py::TestX::test_y - AssertionError",
            "Test",
            "test_failure",
        ),
    ],
)
def test_prefilter_hits(line, expected_extended, expected_old):
    assert (
        cm.classify_tool_output(line, FakeModel(EXTENDED_LABELS, [1 / 8] * 8))
        == expected_extended
    )
    assert (
        cm.classify_tool_output(line, FakeModel(OLD_LABELS, [0.1] * 10)) == expected_old
    )


@pytest.mark.parametrize(
    "line",
    [
        "ERROR: Connection timed out after 30s while connecting to registry:443",
        "Login failed (401 Unauthorized)",
        "ModuleNotFoundError: No module named 'psycopg2'",
        "",
    ],
)
def test_prefilter_misses_plain_logs(line):
    assert (
        cm.classify_tool_output(line, FakeModel(EXTENDED_LABELS, [1 / 8] * 8)) is None
    )


def test_prefilter_majority_vote():
    logs = "\n".join(
        [
            "src/a.py:1:1: F401 'os' imported but unused",
            "src/b.py:2:1: F841 local variable 'x' is assigned to but never used",
            "FAILED tests/test_x.py::TestX::test_y - AssertionError",
        ]
    )
    assert (
        cm.classify_tool_output(logs, FakeModel(EXTENDED_LABELS, [1 / 8] * 8)) == "Lint"
    )


# ---------- predict_category: prefilter + threshold + unknown ----------


def test_predict_category_prefilter_short_circuits_model(monkeypatch):
    _patch_model(monkeypatch, EXTENDED_LABELS, [1 / 8] * 8)
    label, prob, top = cm.predict_category(
        "src/analyzer.py:1:1: F401 'os' imported but unused"
    )
    assert label == "Lint"
    assert prob == cm.PREFILTER_CONFIDENCE
    assert top == [("Lint", cm.PREFILTER_CONFIDENCE)]


def test_predict_category_confident_model_output(monkeypatch):
    probs = [0.02, 0.02, 0.02, 0.02, 0.02, 0.02, 0.02, 0.86]
    _patch_model(monkeypatch, EXTENDED_LABELS, probs)
    label, prob, top = cm.predict_category("some plain error text")
    assert label == "Network"
    assert prob == pytest.approx(0.86, abs=1e-5)
    assert top[0][0] == "Network"


def test_predict_category_rejects_to_unknown_without_llm(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    _patch_model(
        monkeypatch, EXTENDED_LABELS, [0.4, 0.1, 0.1, 0.1, 0.1, 0.05, 0.05, 0.1]
    )
    label, prob, top = cm.predict_category("ambiguous log line")
    assert label == cm.UNKNOWN_LABEL
    assert top[0][0] == cm.UNKNOWN_LABEL


def test_predict_category_llm_adjudication(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    _patch_model(
        monkeypatch, EXTENDED_LABELS, [0.4, 0.1, 0.1, 0.1, 0.1, 0.05, 0.05, 0.1]
    )
    with patch(
        "ml.severity_llm.adjudicate_category", return_value=("Lint", "ruff-style codes")
    ) as adj:
        label, prob, top = cm.predict_category("ambiguous log line")
    assert label == "Lint"
    adj.assert_called_once()


def test_predict_category_llm_failure_falls_back_to_unknown(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    _patch_model(
        monkeypatch, EXTENDED_LABELS, [0.4, 0.1, 0.1, 0.1, 0.1, 0.05, 0.05, 0.1]
    )
    with patch(
        "ml.severity_llm.adjudicate_category", side_effect=RuntimeError("no key")
    ):
        label, _, _ = cm.predict_category("ambiguous log line")
    assert label == cm.UNKNOWN_LABEL


def test_predict_category_logs_structured_fields(monkeypatch, caplog):
    _patch_model(monkeypatch, EXTENDED_LABELS, [0.02] * 7 + [0.86])
    with caplog.at_level("INFO", logger="ml.category_model"):
        cm.predict_category("some plain error text")
    assert any(
        getattr(r, "label", None) == "Network" and getattr(r, "source", None) == "model"
        for r in caplog.records
    )


# ---------- _model_paths ----------


def test_model_paths_prefers_extended_dir(monkeypatch, tmp_path):
    (tmp_path / "model.safetensors").write_bytes(b"x")
    (tmp_path / "tokenizer.json").write_bytes(b"x")
    monkeypatch.setattr(cm, "EXTENDED_DIR", tmp_path)
    base, tok, weights = cm._model_paths()
    assert base == str(tmp_path)
    assert tok == str(tmp_path / "tokenizer.json")
    assert weights == str(tmp_path / "model.safetensors")


# ---------- adjudicate_category ----------


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def _gemini_payload(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def test_adjudicate_category_success(monkeypatch):
    from ml import severity_llm as sl

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    payload = _gemini_payload('{"category": "Lint", "reason": "ruff codes"}')
    with patch("urllib.request.urlopen", return_value=_FakeResp(payload)):
        label, reason = sl.adjudicate_category(
            ["Lint", "Runtime"], "F401 unused import"
        )
    assert label == "Lint"
    assert reason == "ruff codes"


def test_adjudicate_category_unknown(monkeypatch):
    from ml import severity_llm as sl

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    payload = _gemini_payload('{"category": "unknown", "reason": "no match"}')
    with patch("urllib.request.urlopen", return_value=_FakeResp(payload)):
        label, _ = sl.adjudicate_category(["Lint", "Runtime"], "???")
    assert label == "unknown"


def test_adjudicate_category_disabled_provider(monkeypatch):
    from ml import severity_llm as sl

    monkeypatch.setattr(sl, "PROVIDER", "none")
    with pytest.raises(RuntimeError):
        sl.adjudicate_category(["Lint"], "F401")


def test_adjudicate_category_no_candidates():
    from ml import severity_llm as sl

    with pytest.raises(RuntimeError):
        sl.adjudicate_category([], "F401")


def test_predict_category_llm_adjudication_outside_topk(monkeypatch):
    """LLM picks a valid label ranked below top-k: must not crash to unknown."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    probs = [0.5, 0.2, 0.01, 0.1, 0.08, 0.05, 0.03, 0.03]
    _patch_model(monkeypatch, EXTENDED_LABELS, probs)
    with patch("ml.severity_llm.adjudicate_category", return_value=("Lint", "reason")):
        label, prob, _ = cm.predict_category("ambiguous log line")
    assert label == "Lint"
    assert prob == pytest.approx(0.01, abs=1e-5)
