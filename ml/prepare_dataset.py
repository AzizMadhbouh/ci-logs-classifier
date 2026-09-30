import json
import os.path

import datasets
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BASE)
DATA_SRC = os.path.join(REPO, "cicd-selfhealing-dataset")
OUT_DIR = os.path.join(REPO, "data", "ml")
LABEL_COLS = {"category", "component"}


def load_csv(path: str) -> pd.DataFrame:
    return pd.read_csv(path, encoding="utf-8")


def main() -> None:
    splits = {}
    for name in ["train", "val", "test"]:
        df = load_csv(os.path.join(DATA_SRC, f"{name}.csv"))
        df = df[["error_message", "category", "component"]].dropna()
        df = df[df["error_message"].str.strip() != ""]
        splits[name] = df
        print(f"{name}: {len(df)} rows")

    all_labels = {}
    for col in LABEL_COLS:
        values = sorted({v for df in splits.values() for v in df[col].unique()})
        all_labels[col] = {v: i for i, v in enumerate(values)}
    print("label maps:", {k: len(v) for k, v in all_labels.items()})

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "label_maps.json"), "w", encoding="utf-8") as f:
        json.dump(all_labels, f, indent=2)

    for name, df in splits.items():
        df = df.copy()
        df["error_message"] = df["error_message"].str.strip()
        for col in LABEL_COLS:
            df[f"{col}_id"] = df[col].map(all_labels[col])
        ds = datasets.Dataset.from_pandas(df[["error_message", *LABEL_COLS, *(f"{c}_id" for c in LABEL_COLS)]])
        ds.save_to_disk(os.path.join(OUT_DIR, name))
        print(f"wrote {name} split ({len(ds)} rows)")

    ds_dict = datasets.DatasetDict({n: datasets.Dataset.load_from_disk(os.path.join(OUT_DIR, n)) for n in splits})
    print("\ntrain size:", len(ds_dict["train"]), "val size:", len(ds_dict["val"]), "test size:", len(ds_dict["test"]))
    print("first sample:", ds_dict["train"][0])


if __name__ == "__main__":
    main()