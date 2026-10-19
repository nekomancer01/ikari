"""Train the bundled sentiment model on GoEmotions and write src/subsentiment/data/sentiment_model.json.gz.

Needs scikit-learn (training only; the app itself applies the weights in pure Python).
Labels follow the dataset's own sentiment grouping (see tools/eval_sentiment.py if present).

Usage:  python tools/train_model.py [--max-features 40000] [--C 1.0]
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
import urllib.request
from pathlib import Path

import numpy as np
from scipy.sparse import csr_matrix, hstack
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from subsentiment import sentiment  # noqa: E402

BASE = "https://raw.githubusercontent.com/google-research/google-research/master/goemotions/data/"
CACHE = Path(__file__).resolve().parent / ".cache" / "goemotions"
OUT = ROOT / "src" / "subsentiment" / "data" / "sentiment_model.json.gz"
NEUTRAL_ID = 27


def _fetch(name: str) -> Path:
    path = CACHE / name
    if not path.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(BASE + name, path)
    return path


def load(split: str) -> list[tuple[str, str]]:
    emotions = _fetch("emotions.txt").read_text().split()
    groups = json.loads(_fetch("sentiment_dict.json").read_text())
    polarity = {e: g for g, es in groups.items() for e in es}
    rows = []
    with _fetch(f"{split}.tsv").open(encoding="utf-8", newline="") as f:
        for text, ids, _ in csv.reader(f, delimiter="\t"):
            ids = [int(i) for i in ids.split(",")]
            if ids == [NEUTRAL_ID]:
                rows.append((text, "neutral"))
                continue
            pols = {polarity.get(emotions[i]) if i < len(emotions) else "neutral" for i in ids}
            if pols == {"positive"}:
                rows.append((text, "positive"))
            elif pols == {"negative"}:
                rows.append((text, "negative"))
    return rows


def features(rows):
    cleaned = [sentiment.clean(t) for t, _ in rows]
    return cleaned, csr_matrix(np.array([sentiment.vader_features(c) for c in cleaned]))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-features", type=int, default=40000)
    ap.add_argument("--C", type=float, default=1.0)
    args = ap.parse_args()

    train, test = load("train") + load("dev"), load("test")
    ctr, vtr = features(train)
    cte, vte = features(test)
    vec = TfidfVectorizer(ngram_range=(1, 2), min_df=2, sublinear_tf=True, max_features=args.max_features)
    xtr = hstack([vec.fit_transform(ctr), vtr]).tocsr()
    clf = LogisticRegression(C=args.C, max_iter=3000).fit(xtr, [g for _, g in train])

    pred = clf.predict(hstack([vec.transform(cte), vte]).tocsr())
    gold = [g for _, g in test]
    print(f"test accuracy {np.mean([p == g for p, g in zip(pred, gold)]):.4f} (n={len(gold)})")

    n = len(vec.vocabulary_)
    coef = clf.coef_
    vocab = {t: [round(float(vec.idf_[i]), 4)] + [round(float(coef[k, i]), 4) for k in range(len(clf.classes_))]
             for t, i in vec.vocabulary_.items()}
    data = {
        "classes": list(clf.classes_),
        "vocab": vocab,
        "vader_weights": [[round(float(w), 4) for w in coef[k, n:]] for k in range(len(clf.classes_))],
        "bias": [round(float(b), 4) for b in clf.intercept_],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode(), 9, mtime=0))
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KiB, {n} terms)")


if __name__ == "__main__":
    main()
