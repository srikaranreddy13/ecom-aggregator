"""
Build the showcase dashboard from REAL pipeline output.

Runs the actual resolver, the actual TOPSIS, and the actual ablation, then
injects the results into web/template.html and writes dashboard.html.

Nothing in the dashboard is hand-typed. Re-run this after any code change and
the page updates.

    python -m scripts.build_dashboard
"""

import json
import sys
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.adapters.mock_adapter import MockAdapter  # noqa: E402
from src.matching.embedder import Embedder  # noqa: E402
from src.matching.resolver import (  # noqa: E402
    EntityResolver,
    extract_colour,
    extract_generation,
    extract_model_number,
    extract_storage_gb,
)
from src.ranking.mcdm import topsis  # noqa: E402
from scripts.evaluate_matching import pairwise_prf  # noqa: E402

PROFILES = {
    "Budget hunter": {"price": 0.80, "delivery_days": 0.10, "seller_rating": 0.10},
    "In a hurry": {"price": 0.20, "delivery_days": 0.65, "seller_rating": 0.15},
    "Trust first": {"price": 0.20, "delivery_days": 0.15, "seller_rating": 0.65},
}


def build_payload() -> dict:
    rows = json.loads((ROOT / "data" / "sample_listings.json").read_text())
    gold = {f"{r['source']}:{r['source_item_id']}": r["gold_cluster"] for r in rows}

    listings = MockAdapter().search("", limit=1000)
    embedder = Embedder()
    resolver = EntityResolver(embedder)
    clusters = resolver.resolve(listings)

    # ---- clusters with extracted attributes ----
    cluster_out = []
    for c in clusters:
        members = []
        for l in sorted(c.listings, key=lambda x: x.price):
            members.append(
                {
                    "key": l.key,
                    "source": l.source,
                    "id": l.source_item_id,
                    "title": l.title,
                    "price": l.price,
                    "delivery_days": l.delivery_days,
                    "seller_rating": l.seller_rating,
                    "seller_name": l.seller_name,
                    "attrs": {
                        "storage": extract_storage_gb(l.title),
                        "colour": extract_colour(l.title),
                        "model": extract_model_number(l.title),
                        "gen": extract_generation(l.title),
                    },
                }
            )
        prices = [m["price"] for m in members]
        cluster_out.append(
            {
                "id": c.cluster_id,
                "canonical": c.canonical_title,
                "members": members,
                "n": len(members),
                "min_price": min(prices),
                "max_price": max(prices),
                "spread": max(prices) - min(prices),
                "sources": sorted({m["source"] for m in members}),
            }
        )
    cluster_out.sort(key=lambda c: (-c["n"], c["min_price"]))

    # ---- ablation ----
    configs = [
        ("Embeddings only", dict(use_blocking=False, enforce_model_constraint=False)),
        ("Blocking only", dict(use_blocking=True, similarity_threshold=-1.0,
                               enforce_model_constraint=False)),
        ("Hybrid (ours)", dict(use_blocking=True)),
    ]
    ablation = []
    for label, kw in configs:
        cl = EntityResolver(embedder=embedder, **kw).resolve(listings)
        p, r, f1, tp, fp, fn = pairwise_prf(cl, gold)
        ablation.append(
            {"label": label, "precision": round(p, 3), "recall": round(r, 3),
             "f1": round(f1, 3), "tp": tp, "fp": fp, "fn": fn,
             "clusters": len(cl)}
        )

    # ---- normalisation comparison on the largest cluster ----
    target = max(clusters, key=len)
    norm_rows = []
    for label, w in PROFILES.items():
        row = {"profile": label, "weights": w}
        for norm in ("vector", "minmax"):
            best = topsis(target.listings, w, normalisation=norm)[0]
            row[norm] = {
                "source": best.listing.source,
                "price": best.listing.price,
                "delivery_days": best.listing.delivery_days,
                "seller_rating": best.listing.seller_rating,
                "score": best.score,
            }
        norm_rows.append(row)

    # ---- threshold sweep ----
    sweep = []
    for thr in [0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90]:
        cl = EntityResolver(embedder=embedder, similarity_threshold=thr).resolve(listings)
        p, r, f1, *_ = pairwise_prf(cl, gold)
        sweep.append({"thr": thr, "precision": round(p, 3),
                      "recall": round(r, 3), "f1": round(f1, 3)})

    n_tests = len([
        ln for ln in (ROOT / "tests" / "test_pipeline.py")
        .read_text().splitlines() if ln.startswith("def test_")
    ])

    return {
        "meta": {
            "backend": embedder.backend,
            "n_tests": n_tests,
            "n_listings": len(listings),
            "n_clusters": len(clusters),
            "n_gold": len(set(gold.values())),
            "n_sources": len({l.source for l in listings}),
            "threshold": resolver.similarity_threshold,
        },
        "clusters": cluster_out,
        "ablation": ablation,
        "normalisation": norm_rows,
        "sweep": sweep,
        "demo_cluster_id": target.cluster_id,
    }


def main():
    payload = build_payload()
    template = (ROOT / "web" / "template.html").read_text(encoding="utf-8")
    html = template.replace("/*__DATA__*/null", json.dumps(payload, indent=None))
    out = ROOT / "dashboard.html"
    out.write_text(html, encoding="utf-8")

    print(f"  backend        : {payload['meta']['backend']}")
    print(f"  listings       : {payload['meta']['n_listings']}")
    print(f"  clusters       : {payload['meta']['n_clusters']}")
    print(f"  hybrid F1      : {payload['ablation'][-1]['f1']}")
    print(f"\n  wrote {out}")
    print("  open it by double-clicking the file -- no server needed")


if __name__ == "__main__":
    main()
