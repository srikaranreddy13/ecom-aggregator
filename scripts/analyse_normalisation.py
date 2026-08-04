"""
When does vector normalisation actually break TOPSIS?

WHY THIS SCRIPT EXISTS. The README claimed, as a general result, that textbook
(vector-normalised) TOPSIS collapses every shopper profile onto one winner while
min-max keeps the weights responsive. On the 15-listing mock fixture that is
true. On the synthetic market -- which has a wider, more realistic price spread
-- it is FALSE: vector normalisation gives three distinct winners too.

The original claim was not wrong, it was under-specified. The collapse is driven
by the RATIO of coefficients of variation between criteria, because vector
normalisation preserves each criterion's CV while min-max equalises the spans.
When price varies far less than delivery, weighting price cannot compete. When
price varies comparably, it can.

    mock fixture     CV(price)=0.016  CV(delivery)=0.374  ratio ~24x  -> collapses
    synthetic market CV(price)=0.054  CV(delivery)=0.474  ratio ~9x   -> no collapse

So the defensible claim is conditional, and this script measures the condition
rather than asserting the conclusion. That is a stronger result for the paper
than the original blanket statement, and it is the kind of thing an examiner
will probe.

    python -m scripts.analyse_normalisation
"""

from __future__ import annotations

import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.adapters.mock_adapter import MockAdapter  # noqa: E402
from src.adapters.synthetic_adapter import synthetic_market  # noqa: E402
from src.logging_setup import configure_logging  # noqa: E402
from src.matching.embedder import Embedder  # noqa: E402
from src.matching.resolver import EntityResolver, extract_storage_gb  # noqa: E402
from src.ranking.mcdm import topsis, weighted_sum  # noqa: E402

CRITERIA = ("price", "delivery_days", "seller_rating")

PROFILES = {
    "Budget hunter": {"price": 0.80, "delivery_days": 0.10, "seller_rating": 0.10},
    "In a hurry": {"price": 0.20, "delivery_days": 0.65, "seller_rating": 0.15},
    "Trust first": {"price": 0.20, "delivery_days": 0.15, "seller_rating": 0.65},
}


def cv(values) -> float:
    """Coefficient of variation: population sigma / mean."""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return 0.0
    mean = st.mean(vals)
    return (st.pstdev(vals) / mean) if mean else 0.0


def analyse(label: str, listings: list) -> dict:
    cvs = {c: cv(getattr(l, c) for l in listings) for c in CRITERIA}
    ratio = (cvs["delivery_days"] / cvs["price"]) if cvs["price"] else float("inf")

    winners = {}
    for norm in ("minmax", "vector"):
        winners[norm] = [
            topsis(listings, w, normalisation=norm)[0].listing.source
            for w in PROFILES.values()
        ]
    winners["weighted_sum"] = [
        weighted_sum(listings, w)[0].listing.source for w in PROFILES.values()
    ]

    print(f"\n{'=' * 72}")
    print(f"{label}   ({len(listings)} competing offers)")
    print("=" * 72)
    print("  Coefficient of variation per criterion:")
    for c in CRITERIA:
        print(f"    {c:<16} {cvs[c]:.4f}")
    print(f"    CV(delivery)/CV(price) = {ratio:.1f}x")

    print("\n  Distinct winners across the three shopper profiles:")
    for method, w in winners.items():
        n = len(set(w))
        verdict = "responsive" if n == len(PROFILES) else \
                  ("COLLAPSED" if n == 1 else "partly collapsed")
        print(f"    {method:<14} {n}/{len(PROFILES)}  {verdict:<17} {w}")

    return {"label": label, "cvs": cvs, "ratio": ratio, "winners": winners,
            "n_offers": len(listings)}


def main() -> int:
    configure_logging()
    embedder = Embedder()

    # ---- dataset 1: the original hand-built fixture ----
    mock = [l for l in MockAdapter().search("iPhone")
            if extract_storage_gb(l.title) == 128]
    a = analyse("MOCK FIXTURE  (the README's original Result 2)", mock)

    # ---- dataset 2: the synthetic market ----
    listings = []
    for ad in synthetic_market():
        listings.extend(ad.search("iphone", limit=100))
    clusters = EntityResolver(embedder).resolve(listings)
    target = max(clusters, key=len)
    b = analyse("SYNTHETIC MARKET  (wider, more realistic price spread)", target.listings)

    # ---- the conclusion, stated conditionally ----
    print(f"\n{'=' * 72}")
    print("CONCLUSION")
    print("=" * 72)
    print("""
  Vector normalisation preserves each criterion's coefficient of variation;
  min-max equalises the spans. So the textbook method suppresses whichever
  criterion happens to vary least, regardless of the weight placed on it.

  That produces a *collapse* only when the CV gap is large enough for a
  low-variance criterion to be unable to overcome its own compression:""")
    for r in (a, b):
        collapsed = len(set(r["winners"]["vector"])) < len(PROFILES)
        print(f"    CV ratio {r['ratio']:>5.1f}x  ->  vector "
              f"{'COLLAPSES' if collapsed else 'stays responsive'}   ({r['label'].split('  ')[0]})")
    print("""
  Min-max is responsive in BOTH datasets, which is the actual argument for
  making it the default: it does not depend on how the criteria happen to be
  distributed for a given product.

  Do NOT report "vector normalisation collapses the ranking" as a general
  result. Report the mechanism and the condition -- and note that real
  marketplaces sit at whichever CV ratio they sit at, which is exactly why a
  method whose behaviour does not depend on that ratio is preferable.
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
