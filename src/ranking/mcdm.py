"""
Multi-Criteria Decision-Making.

Implements TOPSIS (Technique for Order Preference by Similarity to Ideal
Solution) plus a simple weighted-sum baseline, so you can compare the two in
your evaluation section.

Criteria have a DIRECTION:
    price          -> cost    (lower is better)
    delivery_days  -> cost    (lower is better)
    seller_rating  -> benefit (higher is better)

TOPSIS steps:
    1. Build decision matrix (alternatives x criteria)
    2. Vector-normalise each column
    3. Apply user weights
    4. Find ideal best / ideal worst per criterion (respecting direction)
    5. Score = d_worst / (d_best + d_worst)   -- in [0, 1], higher is better
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

CRITERIA_DIRECTION = {
    "price": "cost",
    "delivery_days": "cost",
    "seller_rating": "benefit",
}

DEFAULT_WEIGHTS = {"price": 0.5, "delivery_days": 0.25, "seller_rating": 0.25}

# used when a source doesn't report a field
IMPUTE = {"price": None, "delivery_days": 10, "seller_rating": 3.0}


@dataclass
class RankedOption:
    listing: object
    score: float
    rank: int
    contributions: dict[str, float]


def _normalise_weights(weights: dict[str, float]) -> dict[str, float]:
    total = sum(max(0.0, w) for w in weights.values())
    if total <= 0:
        raise ValueError("At least one weight must be positive.")
    return {k: max(0.0, v) / total for k, v in weights.items()}


def _build_matrix(listings: list, criteria: list[str]) -> np.ndarray:
    rows = []
    for l in listings:
        row = []
        for c in criteria:
            val = getattr(l, c, None)
            if val is None:
                val = IMPUTE.get(c)
            if val is None:
                raise ValueError(f"Listing {l} missing required criterion {c!r}")
            row.append(float(val))
        rows.append(row)
    return np.array(rows, dtype=np.float64)


def topsis(
    listings: list,
    weights: dict[str, float] | None = None,
    normalisation: str = "minmax",
) -> list[RankedOption]:
    """Rank listings, best first.

    normalisation:
        "minmax" (default) -- each criterion mapped to [0,1] over its observed
            range, direction applied so higher is always better.
        "vector" -- textbook TOPSIS (divide by column L2 norm).

    WHY THE DEFAULT IS NOT THE TEXTBOOK ONE:
        Vector normalisation preserves each criterion's coefficient of
        variation. On real listings, price varies by a few percent across
        sellers while delivery_days varies several-fold. After vector
        normalisation the price column has ~20x less spread than delivery, so
        even a weight of 0.8 on price cannot outrank a weight of 0.1 on
        delivery -- the user's slider stops doing anything.
        Min-max rescales every criterion to the same [0,1] span, so weights
        control influence as the user expects.
        Keep both and report the comparison: it is a real finding.
    """
    if not listings:
        return []

    weights = _normalise_weights(weights or DEFAULT_WEIGHTS)
    criteria = [c for c in weights if weights[c] > 0]
    if not criteria:
        raise ValueError("No active criteria.")

    M = _build_matrix(listings, criteria)

    if len(listings) == 1:
        return [RankedOption(listings[0], 1.0, 1, {c: 1.0 for c in criteria})]

    w = np.array([weights[c] for c in criteria])

    if normalisation == "minmax":
        N = np.zeros_like(M)
        for j, c in enumerate(criteria):
            col = M[:, j]
            lo, hi = col.min(), col.max()
            span = (hi - lo) or 1e-12
            scaled = (col - lo) / span
            if CRITERIA_DIRECTION.get(c, "benefit") == "cost":
                scaled = 1.0 - scaled
            N[:, j] = scaled
        V = N * w
        # after min-max + direction flip, higher is always better
        best_v = V.max(axis=0)
        worst_v = V.min(axis=0)

    elif normalisation == "vector":
        denom = np.sqrt((M ** 2).sum(axis=0))
        denom[denom == 0] = 1.0
        V = (M / denom) * w
        best, worst = [], []
        for j, c in enumerate(criteria):
            col = V[:, j]
            if CRITERIA_DIRECTION.get(c, "benefit") == "cost":
                best.append(col.min())
                worst.append(col.max())
            else:
                best.append(col.max())
                worst.append(col.min())
        best_v, worst_v = np.array(best), np.array(worst)

    else:
        raise ValueError(f"Unknown normalisation {normalisation!r}")

    d_best = np.sqrt(((V - best_v) ** 2).sum(axis=1))
    d_worst = np.sqrt(((V - worst_v) ** 2).sum(axis=1))
    denom2 = d_best + d_worst
    denom2[denom2 == 0] = 1e-12
    scores = d_worst / denom2

    # per-criterion contribution, for the "why this ranked first" UI panel
    order = np.argsort(-scores)
    out: list[RankedOption] = []
    for rank, idx in enumerate(order, start=1):
        contrib = {}
        for j, c in enumerate(criteria):
            span = abs(best_v[j] - worst_v[j]) or 1e-12
            closeness = 1.0 - abs(V[idx, j] - best_v[j]) / span
            contrib[c] = round(float(closeness), 4)
        out.append(
            RankedOption(
                listing=listings[idx],
                score=round(float(scores[idx]), 4),
                rank=rank,
                contributions=contrib,
            )
        )
    return out


def weighted_sum(
    listings: list, weights: dict[str, float] | None = None
) -> list[RankedOption]:
    """Min-max weighted-sum baseline for comparison against TOPSIS."""
    if not listings:
        return []
    weights = _normalise_weights(weights or DEFAULT_WEIGHTS)
    criteria = [c for c in weights if weights[c] > 0]
    M = _build_matrix(listings, criteria)

    scores = np.zeros(len(listings))
    for j, c in enumerate(criteria):
        col = M[:, j]
        lo, hi = col.min(), col.max()
        span = (hi - lo) or 1e-12
        norm = (col - lo) / span
        if CRITERIA_DIRECTION.get(c, "benefit") == "cost":
            norm = 1.0 - norm
        scores += weights[c] * norm

    order = np.argsort(-scores)
    return [
        RankedOption(listings[i], round(float(scores[i]), 4), r, {})
        for r, i in enumerate(order, start=1)
    ]
