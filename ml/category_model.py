"""CI log category classifier (local model files or HuggingFace hub).

10-class ModernBERT classifier for CI log categories:
auth_permission_error, ci_config_git, containers_docker, database_error,
network_api_error, out_of_memory, runtime_error, syntax_error,
test_failure, timeout_error
"""
from pathlib import Path

import torch
from safetensors.torch import load_file
from transformers import AutoConfig

# Repo root (this file lives in <root>/ml/).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Public fallback used when the model files are not present locally
# (Docker image, CI, any machine other than the original dev box).
MODEL_REPO = "ferjaboss/ci-logs-classifier"

_category_tokenizer = None
_category_model = None
_category_device = None


def _model_paths():
    """Return (config_source, tokenizer_file, weights_file).

    Prefers the model files checked out in the project root; otherwise
    downloads them from the public HuggingFace repo into the HF cache.
    """
    local_weights = PROJECT_ROOT / "model.safetensors"
    local_tok = PROJECT_ROOT / "tokenizer.json"
    if local_weights.exists() and local_tok.exists():
        return str(PROJECT_ROOT), str(local_tok), str(local_weights)

    from huggingface_hub import hf_hub_download

    tok_file = hf_hub_download(MODEL_REPO, "tokenizer.json")
    weights_file = hf_hub_download(MODEL_REPO, "model.safetensors")
    return MODEL_REPO, tok_file, weights_file


def get_category_model():
    """Lazy-load tokenizer + model (local files, else HuggingFace hub)."""
    global _category_tokenizer, _category_model, _category_device
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
    return _category_tokenizer, _category_model, _category_device


def predict_category(logs, topk=3):
    """Predict category for a whole job log. Returns (label, prob, topk)."""
    tokenizer, model, device = get_category_model()
    text = logs[:20000]
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
    ids = probs.topk(topk).indices.tolist()
    top = [
        (
            model.config.id2label.get(str(i), model.config.id2label.get(i, str(i))),
            float(probs[i]),
        )
        for i in ids
    ]
    return top[0][0], top[0][1], top
