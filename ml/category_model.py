"""CI log category classifier (local model files in project root).

10-class ModernBERT classifier for CI log categories:
auth_permission_error, ci_config_git, containers_docker, database_error,
network_api_error, out_of_memory, runtime_error, syntax_error,
test_failure, timeout_error
"""
import torch
from safetensors.torch import load_file
from transformers import AutoConfig

PROJECT_ROOT = r"C:\Users\azizm\Documents\Default Project\sales-analyzer"

_category_tokenizer = None
_category_model = None
_category_device = None


def get_category_model():
    """Lazy-load tokenizer + model from local files in project root."""
    global _category_tokenizer, _category_model, _category_device
    if _category_tokenizer is None:
        import torch
        from tokenizers import Tokenizer
        from transformers import AutoModelForSequenceClassification

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        
        # Load tokenizer from tokenizer.json (fast tokenizer)
        tokenizer = Tokenizer.from_file(r"C:\Users\azizm\Documents\Default Project\sales-analyzer\tokenizer.json")
        
        # Load model from config.json + model.safetensors
        config = AutoConfig.from_pretrained(r"C:\Users\azizm\Documents\Default Project\sales-analyzer")
        model = AutoModelForSequenceClassification.from_config(config)
        state_dict = load_file(r"C:\Users\azizm\Documents\Default Project\sales-analyzer\model.safetensors")
        model.load_state_dict(state_dict)
        model.to(torch.device("cuda" if torch.cuda.is_available() else "cpu")).eval()
        
        _category_tokenizer = tokenizer
        _category_model = model
        _category_device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    return _category_tokenizer, _category_model, _category_device


def predict_category(logs, topk=3):
    """Predict category for a whole job log. Returns (label, prob, topk)."""
    tokenizer, model, device = get_category_model()
    text = logs[:20000]
    # Use the fast tokenizer directly
    encoding = _category_tokenizer.encode(text)
    input_ids = torch.tensor([encoding.ids], dtype=torch.long).to(_category_device)
    attention_mask = torch.tensor([encoding.attention_mask], dtype=torch.long).to(_category_device)
    
    with torch.no_grad():
        logits = model(input_ids=input_ids, attention_mask=attention_mask).logits.float()
    probs = torch.softmax(logits, dim=-1)[0]
    ids = probs.topk(topk).indices.tolist()
    top = [(model.config.id2label.get(str(i), model.config.id2label.get(i, str(i))), float(probs[i])) for i in ids]
    return top[0][0], top[0][1], top