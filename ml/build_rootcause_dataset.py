"""Build the root-cause dataset for whole-job CI log classification.

Sources (real datasets, labels used AS-IS - no taxonomy mapping, no synthetic data):
  * TELUS Veloren subset (primary, included):
      - local:    %TEMP%/veloren.csv
      - upstream: ahenrij/intermittent-job-failure-diagnosis -> data/veloren.zip
      - 2,458 whole-job logs, 13 root-cause categories as labeled by the TELUS
        regex-labeling tool (the FlaXifyer priority set).
  * FlakeStorm (optional, gated on HF): ahenrij/flakestorm (~4,200 logs, 30
    classes). Private repo -> skipped unless FLAKESTORM=True and an authorized
    HF token is available. If accessible, its `category` column is used as-is
    and sources are concatenated.

Output: pipeline-dataset/rootcause_dataset.csv
  columns: id, source, logs, category, split(train/val/test)
  splits are stratified by category (seed fd by SEED).
"""
import argparse
import os
import random
import urllib.request
import zipfile

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_CSV = os.path.join(ROOT, "pipeline-dataset", "rootcause_dataset.csv")
LOCAL_VELOREN = os.path.join(os.environ.get("TEMP", ROOT), "veloren.csv")
GITHUB_ZIP = "https://raw.githubusercontent.com/ahenrij/intermittent-job-failure-diagnosis/main/data/veloren.zip"
FLAGSTORM_REPO = "ahenrij/flakestorm"

SEED = 42
TRAIN, VAL, TEST = 0.80, 0.10, 0.10


def fetch_veloren() -> pd.DataFrame:
    """Return the 2,458-row TELUS Veloren subset (id, created_at, logs, category)."""
    if os.path.exists(LOCAL_VELOREN):
        print(f"using local veloren.csv: {LOCAL_VELOREN}")
        return pd.read_csv(LOCAL_VELOREN, encoding="utf-8")
    print("veloren.csv not found locally; downloading from GitHub ...")
    zip_path = os.path.join(os.environ.get("TEMP", ROOT), "veloren_gh.zip")
    urllib.request.urlretrieve(GITHUB_ZIP, zip_path)
    with zipfile.ZipFile(zip_path) as z:
        name = next(n for n in z.namelist() if n.endswith((".csv", "veloren.csv")) and "__MACOSX" not in n)
        csv_path = z.extract(name, os.environ.get("TEMP", ROOT))
        df = pd.read_csv(csv_path, encoding="utf-8")
    if LOCAL_VELOREN and os.path.abspath(csv_path) != os.path.abspath(LOCAL_VELOREN):
        df.to_csv(LOCAL_VELOREN, index=False, encoding="utf-8")
    return df


def fetch_flakestorm() -> pd.DataFrame:
    """Optional: merge FlakeStorm if it is accessible (requires HF login + gate grant)."""
    try:
        from datasets import load_dataset
        ds = load_dataset(FLAGSTORM_REPO, split="train")
    except Exception as e:
        print(f"FlakeStorm unavailable ({type(e).__name__}: {e}); continuing without it.")
        return None
    df = ds.to_pandas()[["logs", "category"]]
    df = df.rename(columns={"logs": "logs"})
    return df[["logs", "category"]]


def stratify(df: pd.DataFrame, seed: int, seed_prefix: str):
    """Append `split` column stratified on category using an independent RNG."""
    rng = random.Random(seed)
    hsh = lambda c: int(__import__("hashlib").sha224((seed_prefix + c).encode()).hexdigest()[:8], 16)
    splits = {}
    for cat, grp in df.groupby("category", sort=False):
        rows = list(grp.index)
        rng.shuffle(rows)
        n = len(rows)
        n_t, n_v = int(TRAIN * n), int(VAL * n)
        splits.update({i: "train" for i in rows[:n_t]})
        splits.update({i: "val" for i in rows[n_t:n_t + n_v]})
        splits.update({i: "test" for i in rows[n_t + n_v:]})
    df["split"] = df.index.map(lambda i: splits.get(i, "train"))
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--flakestorm", action="store_true", help="attempt to include gated HF FlakeStorm")
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()

    veloren = fetch_veloren()
    print(f"veloren: {len(veloren)} rows, {veloren['category'].nunique()} categories")
    veloren = veloren[["logs", "category"]].dropna(subset=["logs", "category"])
    veloren["logs"] = veloren["logs"].str.strip()
    veloren = veloren[veloren["logs"] != ""].drop_duplicates(subset=["logs"])
    veloren["source"] = "veloren"

    frames = [veloren]
    if args.flakestorm:
        fs = fetch_flakestorm()
        if fs is not None:
            fs["logs"] = fs["logs"].str.strip()
            fs = fs[(fs["logs"] != "") & fs["category"].notna()]
            fs = fs.drop_duplicates(subset=["logs"])
            fs["source"] = "flakestorm"
            frames.append(fs)
            print(f"flakestorm: {len(fs)} rows")

    data = pd.concat(frames, ignore_index=True)
    data = data.assign(id=range(len(data)))[["id", "source", "logs", "category"]]

    # exact-duplicate logs across sources keep the first occurrence only
    data = data.drop_duplicates(subset=["logs"], keep="first")
    data = data.reset_index(drop=True)

    stratify(data, args.seed, seed_prefix="rc")
    data["split"] = data["split"].astype("category")
    print("columns:", list(data.columns))
    print("by split:", data["split"].value_counts().to_dict())
    print("by source:", data["source"].value_counts().to_dict())
    print("\ncategory size by split:")
    print(data.pivot_table(index="category", columns="split", values="id", aggfunc="count", fill_value=0).to_string())
    print(f"\nlog length chars: median {int(data['logs'].str.len().median())}, max {int(data['logs'].str.len().max())}")

    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    data.to_csv(OUT_CSV, index=False, encoding="utf-8")
    print(f"\nwritten: {OUT_CSV} ({len(data)} rows)")


if __name__ == "__main__":
    main()