"""Cross-validation evaluation for CI log classifiers.

Provides k-fold CV metrics (accuracy, macro-F1, per-class F1) for both
the category classifier and root-cause classifier using the real datasets.

Usage:
    python -m ml.cross_val_eval --model both --folds 5 --output cv_results.json
    python -m ml.cross_val_eval --model category --category-model ferjaboss/ci-logs-classifier
    python -m ml.cross_val_eval --model rootcause --rootcause-model aziz123123v/cicd-log-rootcause
"""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from sklearn.model_selection import StratifiedKFold
from sklearn.metrics import (
    accuracy_score,
    f1_score,
    classification_report,
    confusion_matrix,
)
from transformers import AutoTokenizer, AutoModelForSequenceClassification
from torch.utils.data import DataLoader
from torch.nn.utils.rnn import pad_sequence

try:
    from tqdm import tqdm
except ImportError:

    def tqdm(iterable, **kwargs):
        return iterable


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "pipeline-dataset"

DEFAULT_CATEGORY_MODEL = "ferjaboss/ci-logs-classifier"
DEFAULT_ROOTCAUSE_MODEL = "aziz123123v/cicd-log-rootcause"

# Local model files (used when HF repo is gated)
LOCAL_CATEGORY_WEIGHTS = PROJECT_ROOT / "model.safetensors"
LOCAL_CATEGORY_TOKENIZER = PROJECT_ROOT / "tokenizer.json"


def load_category_dataset():
    """Load the 10-class category dataset (train_real.csv)."""
    csv_path = DATA_DIR / "train_real.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"Dataset not found: {csv_path}")
    df = pd.read_csv(csv_path, encoding="utf-8")
    df = df[["error_message", "category"]].dropna()
    df = df[df["error_message"].str.strip() != ""]
    return df


def load_rootcause_dataset():
    """Load the root-cause dataset (rootcause_dataset.csv)."""
    csv_path = DATA_DIR / "rootcause_dataset.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"Dataset not found: {csv_path}")
    df = pd.read_csv(csv_path, encoding="utf-8")
    df = df[["logs", "category"]].dropna()
    df = df[df["logs"].str.strip() != ""]
    return df


def window_text(logs, chars=20000):
    """Head+tail windowing (matches rootcause_model.py)."""
    logs = (logs or "").strip()
    if not logs:
        return ""
    if len(logs) <= chars:
        return logs
    half = chars // 2
    return logs[:half] + "\n[......truncated......]\n" + logs[-half:]


class LogDataset(torch.utils.data.Dataset):
    def __init__(self, texts, labels, tokenizer, max_length, window_fn=None):
        self.texts = texts
        self.labels = labels
        self.tokenizer = tokenizer
        self.max_length = max_length
        self.window_fn = window_fn

    def __len__(self):
        return len(self.texts)

    def __getitem__(self, idx):
        text = self.texts[idx]
        if self.window_fn:
            text = self.window_fn(text)
        enc = self.tokenizer(
            text,
            truncation=True,
            padding=False,
            max_length=self.max_length,
            return_tensors="pt",
        )
        return {
            "input_ids": enc["input_ids"].squeeze(0),
            "attention_mask": enc["attention_mask"].squeeze(0),
            "label": torch.tensor(self.labels[idx], dtype=torch.long),
        }


def collate_fn(batch):
    input_ids = pad_sequence(
        [b["input_ids"] for b in batch], batch_first=True, padding_value=0
    )
    attention_mask = pad_sequence(
        [b["attention_mask"] for b in batch], batch_first=True, padding_value=0
    )
    labels = torch.stack([b["label"] for b in batch])
    return {"input_ids": input_ids, "attention_mask": attention_mask, "labels": labels}


def evaluate_fold(model, dataloader, device, id2label):
    """Evaluate model on a single fold, return predictions and true labels."""
    model.eval()
    all_preds = []
    all_labels = []
    with torch.no_grad():
        for batch in tqdm(dataloader, desc="Evaluating", leave=False):
            input_ids = batch["input_ids"].to(device)
            attention_mask = batch["attention_mask"].to(device)
            labels = batch["labels"].to(device)
            logits = model(input_ids=input_ids, attention_mask=attention_mask).logits
            preds = logits.argmax(dim=-1)
            all_preds.extend(preds.cpu().numpy())
            all_labels.extend(labels.cpu().numpy())
    return np.array(all_preds), np.array(all_labels)


def run_cross_validation(
    df,
    text_col,
    label_col,
    model_path,
    num_folds=5,
    max_length=256,
    batch_size=16,
    window_fn=None,
    use_local_category=False,
):
    """Run stratified k-fold cross-validation on a pre-trained model."""
    print(f"\n{'='*60}")
    print(f"Cross-Validation Evaluation: {model_path}")
    print(f"{'='*60}")
    print(f"Dataset: {len(df)} samples | Folds: {num_folds} | Max length: {max_length}")

    labels = sorted(df[label_col].unique())
    label2id = {l: i for i, l in enumerate(labels)}
    id2label = {i: l for i, l in enumerate(labels)}

    texts = df[text_col].tolist()
    y = np.array([label2id[l] for l in df[label_col]])

    skf = StratifiedKFold(n_splits=num_folds, shuffle=True, random_state=42)

    fold_accuracies = []
    fold_macro_f1s = []
    all_preds = []
    all_trues = []

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Device: {device}")

    # Load tokenizer and model ONCE (not per fold)
    if (
        use_local_category
        and LOCAL_CATEGORY_WEIGHTS.exists()
        and LOCAL_CATEGORY_TOKENIZER.exists()
    ):
        print("Loading category model from local files...")
        from tokenizers import Tokenizer
        from transformers import AutoConfig
        from safetensors.torch import load_file

        tokenizer = Tokenizer.from_file(str(LOCAL_CATEGORY_TOKENIZER))
        config = AutoConfig.from_pretrained(str(PROJECT_ROOT))
        model = AutoModelForSequenceClassification.from_config(config)
        model.load_state_dict(load_file(str(LOCAL_CATEGORY_WEIGHTS)))
        model.to(device).eval()

        # Get labels from config
        model_id2label = model.config.id2label
    else:
        tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
        model = (
            AutoModelForSequenceClassification.from_pretrained(
                model_path,
                trust_remote_code=True,
                torch_dtype=torch.float32,
            )
            .to(device)
            .eval()
        )
        model_id2label = model.config.id2label

    # Verify model labels match dataset labels
    if isinstance(model_id2label, dict):
        model_labels = [
            model_id2label.get(str(i), model_id2label.get(i, str(i)))
            for i in range(len(model_id2label))
        ]
    else:
        model_labels = list(model_id2label) if model_id2label else labels

    print(f"Model labels ({len(model_labels)}): {model_labels}")
    print(f"Dataset labels ({len(labels)}): {labels}")

    for fold, (train_idx, val_idx) in enumerate(skf.split(texts, y), 1):
        print(f"\n--- Fold {fold}/{num_folds} ---")

        val_texts = [texts[i] for i in val_idx]
        val_labels = y[val_idx]

        val_dataset = LogDataset(
            val_texts, val_labels, tokenizer, max_length, window_fn
        )
        val_loader = DataLoader(
            val_dataset, batch_size=batch_size, shuffle=False, collate_fn=collate_fn
        )

        preds, trues = evaluate_fold(model, val_loader, device, id2label)

        acc = accuracy_score(trues, preds)
        macro_f1 = f1_score(trues, preds, average="macro", zero_division=0)

        fold_accuracies.append(acc)
        fold_macro_f1s.append(macro_f1)
        all_preds.extend(preds)
        all_trues.extend(trues)

        print(f"  Fold {fold} Accuracy: {acc:.4f} | Macro-F1: {macro_f1:.4f}")

    del model, tokenizer
    torch.cuda.empty_cache()

    all_preds = np.array(all_preds)
    all_trues = np.array(all_trues)

    print(f"\n{'='*60}")
    print("CROSS-VALIDATION RESULTS (Aggregated)")
    print(f"{'='*60}")
    print(
        f"Mean Accuracy:  {np.mean(fold_accuracies):.4f} ± {np.std(fold_accuracies):.4f}"
    )
    print(
        f"Mean Macro-F1:  {np.mean(fold_macro_f1s):.4f} ± {np.std(fold_macro_f1s):.4f}"
    )
    print(f"\nOverall Accuracy: {accuracy_score(all_trues, all_preds):.4f}")
    print(
        f"Overall Macro-F1: {f1_score(all_trues, all_preds, average='macro', zero_division=0):.4f}"
    )

    print("\nPer-Class Metrics:")
    print(
        classification_report(
            all_trues, all_preds, target_names=labels, zero_division=0
        )
    )

    print("Confusion Matrix:")
    cm = confusion_matrix(all_trues, all_preds)
    print(cm)

    return {
        "model_path": model_path,
        "fold_accuracies": [float(x) for x in fold_accuracies],
        "fold_macro_f1s": [float(x) for x in fold_macro_f1s],
        "mean_accuracy": float(np.mean(fold_accuracies)),
        "std_accuracy": float(np.std(fold_accuracies)),
        "mean_macro_f1": float(np.mean(fold_macro_f1s)),
        "std_macro_f1": float(np.std(fold_macro_f1s)),
        "overall_accuracy": float(accuracy_score(all_trues, all_preds)),
        "overall_macro_f1": float(
            f1_score(all_trues, all_preds, average="macro", zero_division=0)
        ),
        "classification_report": classification_report(
            all_trues, all_preds, target_names=labels, zero_division=0, output_dict=True
        ),
        "confusion_matrix": cm.tolist(),
        "labels": labels,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Cross-validation evaluation for CI log classifiers"
    )
    parser.add_argument(
        "--model",
        choices=["category", "rootcause", "both"],
        default="both",
        help="Which model to evaluate",
    )
    parser.add_argument(
        "--category-model",
        type=str,
        default=DEFAULT_CATEGORY_MODEL,
        help=f"Path to category model (default: {DEFAULT_CATEGORY_MODEL})",
    )
    parser.add_argument(
        "--rootcause-model",
        type=str,
        default=DEFAULT_ROOTCAUSE_MODEL,
        help=f"Path to root-cause model (default: {DEFAULT_ROOTCAUSE_MODEL})",
    )
    parser.add_argument("--folds", type=int, default=5, help="Number of CV folds")
    parser.add_argument(
        "--batch-size", type=int, default=16, help="Batch size for evaluation"
    )
    parser.add_argument("--output", type=str, help="Output JSON file for results")
    args = parser.parse_args()

    results = {}

    if args.model in ("category", "both"):
        print("\nLoading category dataset...")
        df_cat = load_category_dataset()
        print(f"Categories: {df_cat['category'].nunique()} | Samples: {len(df_cat)}")

        # Use local files if model path matches default and local files exist
        use_local = (
            args.category_model == DEFAULT_CATEGORY_MODEL
            and LOCAL_CATEGORY_WEIGHTS.exists()
            and LOCAL_CATEGORY_TOKENIZER.exists()
        )
        if use_local:
            print("Using local model files (model.safetensors, tokenizer.json)")

        cat_results = run_cross_validation(
            df_cat,
            text_col="error_message",
            label_col="category",
            model_path=args.category_model,
            num_folds=args.folds,
            max_length=256,
            batch_size=args.batch_size,
            use_local_category=use_local,
        )
        results["category"] = cat_results

    if args.model in ("rootcause", "both"):
        print("\nLoading root-cause dataset...")
        df_rc = load_rootcause_dataset()
        print(f"Root causes: {df_rc['category'].nunique()} | Samples: {len(df_rc)}")

        rc_results = run_cross_validation(
            df_rc,
            text_col="logs",
            label_col="category",
            model_path=args.rootcause_model,
            num_folds=args.folds,
            max_length=4096,
            batch_size=args.batch_size,
            window_fn=window_text,
        )
        results["rootcause"] = rc_results

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        print(f"\nResults saved to {args.output}")

    print("\nDone!")


if __name__ == "__main__":
    main()
