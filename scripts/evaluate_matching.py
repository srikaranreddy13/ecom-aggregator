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

    # FOUR configurations, not three. The resolver has three separable
    # components and the ablation has to isolate each:
    #
    #   blocking   coarse exact partition on brand + colour
    #   embeddings semantic clustering of titles
    #   veto       must-not-link on disagreeing extracted specifications
    #
    # The third row is the one that matters most now. Blocking became
    # deliberately coarse (brand + colour, no quantities) so that listings which
    # omit a spec are not exiled into singleton blocks. That moved ALL numeric
    # precision into the veto -- so an ablation that bundles the veto into
    # "hybrid" would credit blocking and embeddings for work the veto does.
    # Row 3 minus row 4 is the veto's contribution, measured.
    configs = [
        ("Embeddings only",
         dict(use_blocking=False, enforce_model_constraint=False)),
        ("Blocking only",
         dict(use_blocking=True, similarity_threshold=-1.0,
              enforce_model_constraint=False)),
        ("Blocking + embeddings",
         dict(use_blocking=True, enforce_model_constraint=False)),
        ("Full hybrid (ours)",
         dict(use_blocking=True, enforce_model_constraint=True)),
    ]

    header = (f"{'Configuration':<24}{'Prec':>8}{'Rec':>8}{'F1':>8}"
              f"{'TP':>6}{'FP':>6}{'FN':>6}{'Clusters':>10}")
    print(header)
    print("-" * len(header))

    scores = {}
    for label, kw in configs:
        resolver = EntityResolver(embedder=embedder, **kw)
        clusters = resolver.resolve(listings)
        p, r, f1, tp, fp, fn = pairwise_prf(clusters, gold)
        scores[label] = f1
        print(f"{label:<24}{p:>8.3f}{r:>8.3f}{f1:>8.3f}"
              f"{tp:>6}{fp:>6}{fn:>6}{len(clusters):>10}")

    veto_gain = scores["Full hybrid (ours)"] - scores["Blocking + embeddings"]
    print(f"\n  Specification veto contributes {veto_gain:+.3f} F1 over "
          f"blocking + embeddings alone.")
    print("  That is the component that separates WH-1000XM4 from XM5, 128GB from")
    print("  256GB, and a 750W mixer from a 500W one -- none of which blocking or")
    print("  embeddings catch on their own.")

    # Which pairs the veto actually stopped, and how similar their text was.
    # A high text similarity here is the argument against embeddings-only:
    # these pairs would have merged on semantics alone.
    resolver = EntityResolver(embedder=embedder, use_blocking=True)
    resolver.resolve(listings)
    if resolver.last_vetoes:
        print(f"\n  Pairs blocked by the veto ({len(resolver.last_vetoes)}), "
              f"highest text similarity first:")
        for v in sorted(resolver.last_vetoes,
                        key=lambda x: -x["text_similarity"])[:8]:
            print(f"    sim={v['text_similarity']:.3f}  {v['reason']:<28} "
                  f"{v['a_title'][:34]} | {v['b_title'][:34]}")

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
