"""Fine-tune existing category model on train_real.csv with expanded label set.

Preserves 10 original classes from model.safetensors, adds new classes from train_real.csv.
"""

import json
import shutil
from pathlib import Path

import pandas as pd
import torch
from datasets import Dataset
from transformers import (
    AutoConfig,
    AutoModelForSequenceClassification,
    TrainingArguments,
    Trainer,
)
from safetensors.torch import load_file
from tokenizers import Tokenizer

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_WEIGHTS = PROJECT_ROOT / "model.safetensors"
LOCAL_TOKENIZER = PROJECT_ROOT / "tokenizer.json"
DATA_PATH = PROJECT_ROOT / "pipeline-dataset" / "train_real.csv"
OUTPUT_DIR = PROJECT_ROOT / "category_model_finetuned"

MAX_LENGTH = 256
BATCH_SIZE = 16
EPOCHS = 3
LR = 1e-5


def main():
    # 1. Load data + build unified label map
    print("Loading train_real.csv...")
    df = pd.read_csv(DATA_PATH, encoding="utf-8")
    df = df[["error_message", "category"]].dropna()
    df = df[df["error_message"].str.strip() != ""]
    print(f"Loaded {len(df)} samples, {df['category'].nunique()} categories")

    # Get existing model labels
    config = AutoConfig.from_pretrained(str(PROJECT_ROOT))
    _id2l = config.id2label
    existing_labels = [
        _id2l[i] if i in _id2l else _id2l[str(i)] for i in range(config.num_labels)
    ]
    print(f"Existing model labels ({len(existing_labels)}): {existing_labels}")

    # Add new labels from train_real.csv
    new_labels = sorted(set(df["category"].unique()) - set(existing_labels))
    all_labels = existing_labels + new_labels
    label2id = {l: i for i, l in enumerate(all_labels)}
    id2label = {i: l for i, l in enumerate(all_labels)}
    print(f"New labels to add ({len(new_labels)}): {new_labels}")
    print(f"Total labels: {len(all_labels)}")

    # 2. Load tokenizer + model, expand head
    print("\nLoading tokenizer and model...")
    tokenizer = Tokenizer.from_file(str(LOCAL_TOKENIZER))
    model = AutoModelForSequenceClassification.from_config(config)
    model.load_state_dict(load_file(str(LOCAL_WEIGHTS)))

    # Expand classifier head
    old_head = model.classifier
    in_features = old_head.in_features
    model.classifier = torch.nn.Linear(in_features, len(all_labels))

    # Copy old weights, initialize new
    with torch.no_grad():
        model.classifier.weight[: len(existing_labels)] = old_head.weight
        model.classifier.bias[: len(existing_labels)] = old_head.bias
    torch.nn.init.xavier_uniform_(model.classifier.weight[len(existing_labels) :])
    model.classifier.bias[len(existing_labels) :].zero_()

    model.config.id2label = id2label
    model.config.label2id = label2id
    model.config.num_labels = len(all_labels)
    print(f"Model head expanded: {len(existing_labels)} -> {len(all_labels)} classes")

    # 3. Prepare dataset
    print("Tokenizing dataset...")

    def tokenize(batch):
        return tokenizer(
            batch["error_message"],
            truncation=True,
            padding="max_length",
            max_length=MAX_LENGTH,
        )

    ds = Dataset.from_pandas(df.reset_index(drop=True))
    ds = ds.map(lambda x: {"label": label2id[x["category"]]})
    ds = ds.map(tokenize, batched=True, remove_columns=["error_message", "category"])
    ds.set_format("torch", columns=["input_ids", "attention_mask", "label"])

    # Stratified split
    split_ds = ds.train_test_split(test_size=0.2, stratify_by_column="label", seed=42)
    train_ds = split_ds["train"]
    val_ds = split_ds["test"]
    print(f"Train: {len(train_ds)} | Val: {len(val_ds)}")

    # 4. Training arguments
    args = TrainingArguments(
        output_dir=str(OUTPUT_DIR),
        num_train_epochs=EPOCHS,
        per_device_train_batch_size=BATCH_SIZE,
        per_device_eval_batch_size=BATCH_SIZE * 2,
        learning_rate=LR,
        warmup_steps=50,
        weight_decay=0.01,
        eval_strategy="epoch",
        save_strategy="epoch",
        load_best_model_at_end=True,
        metric_for_best_model="accuracy",
        greater_is_better=True,
        fp16=torch.cuda.is_available(),
        logging_steps=25,
        report_to=[],
        seed=42,
    )

    def compute_metrics(eval_pred):
        logits, labels = eval_pred
        preds = logits.argmax(axis=-1)
        acc = (preds == labels).mean()
        return {"accuracy": float(acc)}

    trainer = Trainer(
        model=model,
        args=args,
        train_dataset=train_ds,
        eval_dataset=val_ds,
        compute_metrics=compute_metrics,
    )

    # 5. Train
    print("\nStarting fine-tuning...")
    trainer.train()

    # 6. Save
    print(f"\nSaving to {OUTPUT_DIR}...")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(OUTPUT_DIR)
    shutil.copy(LOCAL_TOKENIZER, OUTPUT_DIR / "tokenizer.json")
    with open(OUTPUT_DIR / "label_maps.json", "w") as f:
        json.dump({"label2id": label2id, "id2label": id2label}, f, indent=2)
    print("Done!")


if __name__ == "__main__":
    main()
