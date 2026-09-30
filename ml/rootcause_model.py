"""Whole-job CI log root-cause classification.

Single-head ModernBERT sequence classifier fine-tuned on real whole-job logs
(TELUS Veloren subset: 13 root-cause categories, labels used as-is). Input is a
whole build job log; output is the job's root-cause label + calibrated confidence.

Also exports `window_text`, the head+tail windowing used at train AND predict time
(kept in sync with ml/rootcause_training.ipynb).
"""
import re

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

MAX_LEN = 4096
WINDOW_CHARS = 20000
DEFAULT_REPO = "aziz123123v/cicd-log-rootcause"

_ROOT_CAUSE_BASELINE = {
    "api_gateway_deployment_error": "High",
    "container_registry_server_error": "High",
    "dependency_installation_failure": "High",
    "helm_resource_error": "High",
    "misconfigured_env_variable": "High",
    "external_file_invalid_format": "Medium",
    "flaky_ui_test": "Medium",
    "git_transient_error": "Medium",
    "host_resolution_failure": "Medium",
    "job_execution_timeout": "Medium",
    "remote_call_timeout": "Medium",
    "runner_image_pull_failure": "Medium",
    "runner_pod_waiting_timeout": "Medium",
}


def window_text(logs, chars=WINDOW_CHARS):
    """Head+tail window of a raw job log (most CI failure evidence sits at the end)."""
    logs = (logs or "").strip()
    if not logs:
        return ""
    if len(logs) <= chars:
        return logs
    half = chars // 2
    return logs[:half] + "\n[......truncated......]\n" + logs[-half:]


def load(repo_id=DEFAULT_REPO, device=None):
    """Load tokenizer + model (AutoModelForSequenceClassification; id2label in config)."""
    if device is None:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    tokenizer = AutoTokenizer.from_pretrained(repo_id, trust_remote_code=True)
    model = AutoModelForSequenceClassification.from_pretrained(
        repo_id,
        torch_dtype=torch.float32,
        trust_remote_code=True,
    ).to(device).eval()
    return tokenizer, model, device


def predict_log(tokenizer, model, device, logs, topk=3):
    """Root-cause prediction for a whole job log. Returns (label, prob, top3)."""
    text = window_text(logs)
    enc = tokenizer(
        text, truncation=True, padding=True, max_length=MAX_LEN, return_tensors="pt",
    )
    enc = {k: v.to(device) for k, v in enc.items()}
    with torch.no_grad():
        logits = model(**enc).logits.float()
    probs = torch.softmax(logits, dim=-1)[0]
    ids = probs.topk(topk).indices.tolist()
    top = [(model.config.id2label.get(str(i), model.config.id2label.get(i, str(i))), float(probs[i])) for i in ids]
    return top[0][0], top[0][1], top


_ERROR_RE = re.compile(
    r"error|fail|fatal|exception|timed? out|timeout|refused|denied|not found|cannot?|unable|no space|panic", re.IGNORECASE
)


def extract_evidence_lines(text, limit=15, max_chars=8000):
    """Deduplicated, order-preserving error/warning lines used as evidence and LLM fuel."""
    seen, out = set(), []
    for raw in text.splitlines():
        s = re.sub(r"\x1b\[[0-9;]*m", "", raw)
        s = re.sub(r"ha://\S+", "", s).strip()
        if not s or s in seen:
            continue
        if not _ERROR_RE.search(s):
            continue
        if s.startswith("[Pipeline]") or s.startswith("Running on") or re.match(r"^\s*\[", s):
            continue
        if s.startswith("> ") or "# timeout=" in s:  # Jenkins git/checkout command echoes
            continue
        seen.add(s)
        out.append(s[:300])
        if len(out) >= limit:
            break
    block = "\n".join(out)
    return out, block[:max_chars]


if __name__ == "__main__":
    import sys
    tokenizer, model, device = load()
    with open(sys.argv[1], encoding="utf-8-sig", errors="replace") as f:
        log_text = f.read()
    label, prob, top = predict_log(tokenizer, model, device, log_text)
    print(f"root cause: {label}  (confidence {prob:.3f})")
    for l, p in top[1:]:
        print(f"  alt: {l} ({p:.3f})")