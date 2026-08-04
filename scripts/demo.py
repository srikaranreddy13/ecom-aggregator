"""
End-to-end demo. Run this in front of your supervisor.

    python -m scripts.demo
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.adapters.mock_adapter import MockAdapter  # noqa: E402
from src.matching.embedder import Embedder  # noqa: E402
from src.matching.resolver import EntityResolver  # noqa: E402
from src.pipeline import AggregatorPipeline  # noqa: E402
from src.storage import db  # noqa: E402

PROFILES = {
    "Budget hunter":   {"price": 0.80, "delivery_days": 0.10, "seller_rating": 0.10},
    "In a hurry":      {"price": 0.20, "delivery_days": 0.65, "seller_rating": 0.15},
    "Trust-first":     {"price": 0.20, "delivery_days": 0.15, "seller_rating": 0.65},
}

BAR = "=" * 74


def main():
    conn = db.connect()
    adapters = [
        MockAdapter(only_source=s) for s in ("alpha", "beta", "gamma")
    ]
    pipeline = AggregatorPipeline(
        adapters=adapters,
        resolver=EntityResolver(Embedder()),
        conn=conn,
        # The demo must NOT write price history. It reads MockAdapter, which
        # serves a static file, so every run would append another zero-variance
        # observation -- and those flat rows are exactly what made the original
        # price_history table unforecastable. Collection is a scheduled job
        # (scripts/collect_prices.py), not a side effect of demoing.
        record_history=False,
    )

    print(BAR)
    print("STAGE 1 -- INGESTION  (3 independent sources via SourceAdapter)")
    print(BAR)
    raw = pipeline.fetch("")
    for a in adapters:
        n = len([l for l in raw if l.source == a.name or True])
    from collections import Counter
    for src, n in sorted(Counter(l.source for l in raw).items()):
        print(f"  {src:<8} -> {n:>2} listings")
    print(f"  {'TOTAL':<8} -> {len(raw):>2} listings")

    print()
    print(BAR)
    print("STAGE 2 -- SEMANTIC ENTITY RESOLUTION")
    print(BAR)
    clusters = pipeline.resolver.resolve(raw)
    print(f"  {len(raw)} raw listings collapsed into {len(clusters)} distinct products\n")
    for c in clusters:
        srcs = ",".join(sorted({l.source for l in c.listings}))
        print(f"  [{len(c)} offers | {srcs}]  {c.canonical_title}")
        for l in c.listings:
            print(f"        {l.source:<7} Rs.{l.price:>9,.0f}  "
                  f"{str(l.delivery_days)+'d':<4}  {l.seller_rating}*  {l.title[:44]}")
        print()

    print(BAR)
    print("STAGE 3 -- MCDM: same product, different user priorities")
    print(BAR)
    target = max(clusters, key=len)
    print(f"  Product: {target.canonical_title}\n")
    for label, weights in PROFILES.items():
        results = pipeline.search("", weights=weights)
        match = next(r for r in results if r["cluster_id"] == target.cluster_id)
        b = match["best_offer"]
        why = ", ".join(f"{k}={v:.2f}" for k, v in b["why"].items())
        print(f"  {label:<15} -> {b['source']:<7} "
              f"Rs.{b['price']:>9,.0f}  {b['delivery_days']}d  "
              f"{b['seller_rating']}*   score={b['topsis_score']:.3f}")
        print(f"  {'':<15}    ({why})")
    print()
    print("  ^ The winner CHANGES with the weight vector. That is the MCDM")
    print("    module working -- there is no single 'best' offer.\n")

    print(BAR)
    print("STAGE 4 -- PRICE HISTORY")
    print(BAR)
    stats = db.series_stats(conn)
    print(f"  observations       : {stats['observations']}")
    print(f"  distinct days      : {stats['distinct_days']}")
    print(f"  tracked items      : {stats['tracked_items']}")
    print(f"  synthetic / real   : {stats['synthetic_observations']} / "
          f"{stats['real_observations']}")

    if stats["flat_series"]:
        print(f"  flat series        : {stats['flat_series']}  <- zero price "
              f"variance, cannot be forecast")

    if not stats["observations"]:
        print("\n  No history yet. Run:  python -m scripts.backfill_prices --days 180")
    else:
        items = db.tracked_items(conn, min_observations=10)
        if items:
            it = items[0]
            series = db.price_series(conn, it["source"], it["source_item_id"])
            spread = (it["max_price"] - it["min_price"]) / it["avg_price"] * 100
            print(f"\n  Longest series: {it['source']} / {it['source_item_id']}")
            print(f"    {it['title'][:64]}")
            print(f"    {len(series)} observations, "
                  f"Rs.{it['min_price']:,.0f} -- Rs.{it['max_price']:,.0f} "
                  f"(spread {spread:.1f}% of mean)")
            print("    last 10: " +
                  ", ".join(f"{d[5:]}={p:,.0f}" for d, p in series[-10:]))
            print("\n  ^ The price MOVES. That is what makes forecasting a")
            print("    well-posed problem. An earlier version of the collector")
            print("    recorded a static file daily and produced a flat line.")

    if stats["synthetic_observations"] and not stats["real_observations"]:
        print("\n  NOTE: this history is SIMULATED (is_synthetic=1). It proves the")
        print("  forecasting pipeline works; it says nothing about real-market")
        print("  accuracy. Configure eBay credentials and schedule")
        print("  scripts/collect_prices.py to accumulate genuine observations.\n")


if __name__ == "__main__":
    main()
