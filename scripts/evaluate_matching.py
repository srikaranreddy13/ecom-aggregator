"""
Entity-resolution evaluation with ablation.

This script produces the table that goes in your paper. It measures pairwise
precision / recall / F1 against the hand-labelled `gold_cluster` field, under
three configurations:

    embeddings only   -- no attribute blocking
    blocking only     -- exact attribute match, no semantics
    hybrid (ours)     -- blocking + embeddings

Run:  python -m scripts.evaluate_matching
"""

import json
import sys
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.adapters.mock_adapter import MockAdapter  # noqa: E402
from src.matching.embedder import Embedder  # noqa: E402
from src.matching.resolver import EntityResolver  # noqa: E402


def pairwise_prf(clusters, gold_by_key):
    """Pairwise precision/recall/F1 over all listing pairs."""
    pred_pairs = set()
    for c in clusters:
        for a, b in combinations(sorted(l.key for l in c.listings), 2):
            pred_pairs.add((a, b))

    by_gold = {}
    for key, g in gold_by_key.items():
        by_gold.setdefault(g, []).append(key)
    true_pairs = set()
    for keys in by_gold.values():
        for a, b in combinations(sorted(keys), 2):
            true_pairs.add((a, b))

    tp = len(pred_pairs & true_pairs)
    fp = len(pred_pairs - true_pairs)
    fn = len(true_pairs - pred_pairs)

    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return prec, rec, f1, tp, fp, fn


def main():
    rows = json.loads((ROOT / "data" / "sample_listings.json").read_text())
    gold = {f"{r['source']}:{r['source_item_id']}": r["gold_cluster"] for r in rows}

    listings = MockAdapter().search("", limit=1000)
    embedder = Embedder()

    print(f"\nEmbedding backend : {embedder.backend}")
    print(f"Listings          : {len(listings)}")
    print(f"Gold clusters     : {len(set(gold.values()))}\n")

    configs = [
        ("Embeddings only", dict(use_blocking=False, enforce_model_constraint=False)),
        ("Blocking only",   dict(use_blocking=True,  similarity_threshold=-1.0, enforce_model_constraint=False)),
        ("Hybrid (ours)",   dict(use_blocking=True)),
    ]

    header = f"{'Configuration':<20}{'Prec':>8}{'Rec':>8}{'F1':>8}{'TP':>6}{'FP':>6}{'FN':>6}"
    print(header)
    print("-" * len(header))

    for label, kw in configs:
        resolver = EntityResolver(embedder=embedder, **kw)
        clusters = resolver.resolve(listings)
        p, r, f1, tp, fp, fn = pairwise_prf(clusters, gold)
        print(f"{label:<20}{p:>8.3f}{r:>8.3f}{f1:>8.3f}{tp:>6}{fp:>6}{fn:>6}")

    # threshold sweep -- gives you a figure for the paper
    print("\nThreshold sweep (hybrid):")
    print(f"{'thr':>6}{'Prec':>8}{'Rec':>8}{'F1':>8}")
    for thr in [0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90]:
        res = EntityResolver(embedder=embedder, similarity_threshold=thr)
        p, r, f1, *_ = pairwise_prf(res.resolve(listings), gold)
        print(f"{thr:>6.2f}{p:>8.3f}{r:>8.3f}{f1:>8.3f}")
    print()


if __name__ == "__main__":
    main()
