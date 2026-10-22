"""Evaluate subsentiment's VADER scoring against GoEmotions (Reddit comments).

GoEmotions (Demszky et al., 2020) is ~58k English Reddit comments annotated with
27 emotions + neutral. We collapse them to the app's three labels with the
dataset's own sentiment grouping: a comment is positive/negative if all its
emotions are positive/negative, neutral if it is labelled neutral only. Comments
with ambiguous emotions (surprise, curiosity, ...) or mixed polarity are dropped.

Usage:
    python tools/eval_sentiment.py                    # report on the test split
    python tools/eval_sentiment.py --split dev
    python tools/eval_sentiment.py --tune             # fit thresholds on dev, report on test
    python tools/eval_sentiment.py --sarcasm          # also score the sarcasm slice (iSarcasmEval)

The sarcasm slice is iSarcasmEval (Abu Farha et al., 2022): tweets their authors
marked sarcastic, each with the author's own plain rephrasing. The rephrasing's
label stands in for the intended sentiment.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.request
from pathlib import Path

from sklearn.metrics import classification_report, confusion_matrix, f1_score

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from subsentiment import sentiment  # noqa: E402
from subsentiment.analysis import LABELS  # noqa: E402

BASE = "https://raw.githubusercontent.com/google-research/google-research/master/goemotions/data/"
CACHE = Path(__file__).resolve().parent / ".cache" / "goemotions"
ISARCASM = "https://raw.githubusercontent.com/iabufarha/iSarcasmEval/main/train/train.En.csv"
NEUTRAL_ID = 27  # emotions.txt lists 27 emotions; id 27 is "neutral"


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


def load_sarcasm() -> list[tuple[str, str]]:
    """(sarcastic tweet, its author's literal rephrasing) pairs."""
    path = CACHE.parent / "isarcasmeval-train.En.csv"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(ISARCASM, path)
    with path.open(encoding="utf-8", newline="") as f:
        return [(r["tweet"], r["rephrase"]) for r in csv.DictReader(f)
                if r["sarcastic"] == "1" and r["tweet"].strip() and r["rephrase"].strip()]


def labels(rows, sarcasm: bool = True) -> list[str]:
    """The app's labels; sarcasm=False turns off the sarcasm flip for comparison."""
    original = sentiment.is_sarcastic
    if not sarcasm:
        sentiment.is_sarcastic = lambda cleaned: False
    try:
        return [sentiment.score(t)[1] for t, _ in rows]
    finally:
        sentiment.is_sarcastic = original


def compounds(rows) -> list[float]:
    return [sentiment.score(t)[0] for t, _ in rows]


def predict(scores, pos: float, neg: float) -> list[str]:
    return ["positive" if s >= pos else "negative" if s <= neg else "neutral" for s in scores]


def macro_f1(gold, pred) -> float:
    return f1_score(gold, pred, labels=LABELS, average="macro", zero_division=0)


def tune(scores, gold):
    best = (-1.0, 0.0, 0.0)
    grid = [i / 100 for i in range(0, 61, 2)]
    for pos in grid:
        for neg in grid:
            f = macro_f1(gold, predict(scores, pos, -neg))
            if f > best[0]:
                best = (f, pos, -neg)
    return best


_VALUE = {"negative": -1.0, "neutral": 0.0, "positive": 1.0}


def aggregate(gold, pred) -> dict:
    """Errors that matter when you read results in bulk, not item by item."""
    n = len(gold)
    share = lambda xs, l: xs.count(l) / n  # noqa: E731
    per_label = {l: share(pred, l) - share(gold, l) for l in LABELS}
    return {
        "per_label": per_label,
        "tv_distance": sum(abs(v) for v in per_label.values()) / 2,
        "mean_gold": sum(_VALUE[g] for g in gold) / n,
        "mean_error": sum(_VALUE[p] for p in pred) / n - sum(_VALUE[g] for g in gold) / n,
    }


def report(title, gold, pred):
    acc = sum(g == p for g, p in zip(gold, pred)) / len(gold)
    print(f"\n== {title} ==  n={len(gold)}  accuracy={acc:.4f}  macro-F1={macro_f1(gold, pred):.4f}")
    a = aggregate(gold, pred)
    shares = "  ".join(f"{l} {v:+.3f}" for l, v in a["per_label"].items())
    print(f"aggregate: label-share error [{shares}]  total-variation {a['tv_distance']:.3f}  "
          f"mean sentiment {a['mean_error']:+.3f} (gold {a['mean_gold']:+.3f})")
    print(classification_report(gold, pred, labels=LABELS, digits=3, zero_division=0))
    cm = confusion_matrix(gold, pred, labels=LABELS)
    print("confusion (rows=gold, cols=pred):", LABELS)
    for lab, row in zip(LABELS, cm):
        print(f"  {lab:9s}", *[f"{v:6d}" for v in row])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--split", default="test", choices=["train", "dev", "test"])
    ap.add_argument("--model", action="store_true",
                    help="also train a TF-IDF + VADER-feature logistic regression on train and report it")
    ap.add_argument("--tune", action="store_true", help="fit thresholds on dev, report on --split")
    ap.add_argument("--sarcasm", action="store_true", help="also report on the iSarcasmEval sarcasm slice")
    args = ap.parse_args()

    rows = load(args.split)
    gold = [g for _, g in rows]
    scores = compounds(rows)
    dist = {l: gold.count(l) for l in LABELS}
    print(f"GoEmotions {args.split}: {dist}")
    report(f"baseline (±{sentiment.POSITIVE_THRESHOLD})", gold,
           predict(scores, sentiment.POSITIVE_THRESHOLD, sentiment.NEGATIVE_THRESHOLD))
    report("app labels, sarcasm flip off", gold, labels(rows, sarcasm=False))
    report("app labels", gold, labels(rows))
    if args.sarcasm:
        pairs = load_sarcasm()
        intended = labels([(r, None) for _, r in pairs], sarcasm=False)
        print(f"\niSarcasmEval sarcastic tweets: n={len(pairs)}, intended (rephrase) labels "
              + "  ".join(f"{l} {intended.count(l) / len(pairs):.3f}" for l in LABELS))
        for title, on in (("sarcasm flip off", False), ("sarcasm flip on", True)):
            pred = labels(pairs, sarcasm=on)
            agree = sum(p == g for p, g in zip(pred, intended)) / len(pairs)
            print(f"  {title:16s} agreement with intended {agree:.3f}  predicted "
                  + "  ".join(f"{l} {pred.count(l) / len(pairs):.3f}" for l in LABELS))
    if args.tune:
        dev = load("dev")
        f, pos, neg = tune(compounds(dev), [g for _, g in dev])
        print(f"\nthresholds fitted on dev: pos>={pos:.2f}, neg<={neg:.2f} (dev macro-F1 {f:.4f})")
        report(f"tuned thresholds on {args.split}", gold, predict(scores, pos, neg))
    if args.model:
        report(f"TF-IDF + VADER logistic regression (trained on train) on {args.split}",
               gold, model_predictions(load("train"), rows))


def model_predictions(train, test):
    """Local offline classifier: word/bigram TF-IDF plus VADER's own scores as features."""
    import numpy as np
    from scipy.sparse import csr_matrix, hstack
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression

    def vader(rows):
        out = []
        for t, _ in rows:
            c = sentiment.clean(t)
            p = sentiment._analyzer().polarity_scores(c) if c else {"neg": 0, "neu": 1, "pos": 0, "compound": 0}
            out.append([p["neg"], p["neu"], p["pos"], p["compound"], abs(p["compound"])])
        return csr_matrix(np.array(out))

    vec = TfidfVectorizer(ngram_range=(1, 2), min_df=2, sublinear_tf=True)
    xtr = hstack([vec.fit_transform([t for t, _ in train]), vader(train)]).tocsr()
    xte = hstack([vec.transform([t for t, _ in test]), vader(test)]).tocsr()
    clf = LogisticRegression(C=1, max_iter=3000).fit(xtr, [g for _, g in train])
    return list(clf.predict(xte))


if __name__ == "__main__":
    main()
