"""
Recommendations: "customers who bought this also bought".

THE PROBLEM WITH THE TEXTBOOK ANSWER. Item-item collaborative filtering needs an
interaction matrix -- who bought what -- and this project has no users. The usual
student shortcut is to invent random purchases, but random co-purchase data
produces random recommendations that merely *look* like a working recommender.
That is worse than nothing, because it is indistinguishable from the real thing
until someone reads the output carefully.

So relevance is built from three signals that need no user data, blended:

  1. COMPLEMENT  (weight 1.0)  Cross-category rules: a laptop goes with a bag,
     a phone with a charger, a dress with shoes. These are the recommendations
     that read as genuinely useful, because complementary purchase is what
     "also bought" actually means in retail. Defined per CATEGORY PAIR, not per
     product, so a new product inherits them for free.

  2. SUBSTITUTE  (weight 0.55) Same category, similar price band. What a shopper
     compares against before deciding. Deliberately ranked BELOW complements:
     a page full of near-identical alternatives is a comparison tool, not a
     recommendation.

  3. AFFINITY    (weight 0.35) Shared brand or tags. Weakest signal, used to
     break ties, because same-brand is a weak reason on its own.

Every candidate is then filtered: never recommend the product itself, never
recommend something whose price is absurd relative to it (a Rs.9,000 case for a
Rs.2,000 phone), and never fill space with padding. Returning three good
suggestions beats returning ten with seven duds -- the user asked for relevant
recommendations only.

HONEST LIMITATION, state it in the review: with no interaction data this is a
content-and-rules recommender, not collaborative filtering. It cannot discover
the non-obvious pairings that CF finds from real behaviour ("people who buy this
printer buy that specific off-brand toner"). The upgrade path is a public
interaction dataset (Amazon Reviews, RetailRocket); `score_from_cooccurrence` is
where those counts would enter, and the blend already has a slot for it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .catalogue import Product
from .logging_setup import get_logger

log = get_logger(__name__)

WEIGHT_COMPLEMENT = 1.00
WEIGHT_SUBSTITUTE = 0.55
WEIGHT_AFFINITY = 0.35
WEIGHT_COOCCURRENCE = 1.20      # reserved: real behaviour outranks every rule

# A complement priced wildly out of proportion to the anchor is noise. A phone
# case at 30% of the phone's price is plausible; at 300% it is a mis-suggestion.
MAX_COMPLEMENT_PRICE_RATIO = 1.5
# Substitutes must be genuinely comparable, or the "alternatives" are useless.
SUBSTITUTE_PRICE_BAND = (0.4, 2.5)


@dataclass
class Recommendation:
    product: Product
    score: float
    reason: str          # human-readable, shown in the UI
    kind: str            # complement | substitute | affinity

    def to_dict(self) -> dict:
        d = self.product.to_dict()
        d.update({"score": round(self.score, 4), "reason": self.reason,
                  "kind": self.kind})
        return d


class Recommender:
    """Blends complement rules, substitutes and affinity over a catalogue."""

    def __init__(self, products: list[Product],
                 cooccurrence: dict[tuple[str, str], int] | None = None):
        self.products = products
        self.by_id = {p.product_id: p for p in products}
        self.by_category: dict[str, list[Product]] = {}
        for p in products:
            if p.category:
                self.by_category.setdefault(p.category, []).append(p)
        for bucket in self.by_category.values():
            bucket.sort(key=lambda p: -(p.rating or 0))

        # Slot for real interaction data. Empty until a behavioural dataset is
        # loaded; the blend already accounts for it so wiring one in later needs
        # no change to the scoring logic.
        self.cooccurrence = cooccurrence or {}

    # ---------------- individual signals ----------------

    def score_from_cooccurrence(self, anchor_id: str, other_id: str) -> float:
        """Normalised co-purchase strength. Zero until real data exists."""
        if not self.cooccurrence:
            return 0.0
        pair = (anchor_id, other_id) if anchor_id < other_id else (other_id, anchor_id)
        count = self.cooccurrence.get(pair, 0)
        if not count:
            return 0.0
        best = max(self.cooccurrence.values())
        return count / best if best else 0.0

    def _complements(self, anchor: Product) -> list[Recommendation]:
        out = []
        for pid in anchor.complements:
            candidate = self.by_id.get(pid)
            if candidate is None or candidate.product_id == anchor.product_id:
                continue
            if anchor.reference_price > 0:
                ratio = candidate.reference_price / anchor.reference_price
                if ratio > MAX_COMPLEMENT_PRICE_RATIO:
                    continue
            # Higher-rated complements first; rating is the only quality signal
            # the catalogue gives us.
            quality = (candidate.rating or 3.0) / 5.0
            out.append(Recommendation(
                product=candidate,
                score=WEIGHT_COMPLEMENT * quality,
                reason=f"Frequently bought with {anchor.category or 'this'}",
                kind="complement",
            ))
        return out

    def _substitutes(self, anchor: Product) -> list[Recommendation]:
        if not anchor.category:
            return []
        lo, hi = SUBSTITUTE_PRICE_BAND
        out = []
        for candidate in self.by_category.get(anchor.category, []):
            if candidate.product_id == anchor.product_id:
                continue
            if anchor.reference_price > 0:
                ratio = candidate.reference_price / anchor.reference_price
                if not (lo <= ratio <= hi):
                    continue
            quality = (candidate.rating or 3.0) / 5.0
            out.append(Recommendation(
                product=candidate,
                score=WEIGHT_SUBSTITUTE * quality,
                reason="Shoppers also compared this",
                kind="substitute",
            ))
        return out

    def _affinity(self, anchor: Product) -> list[Recommendation]:
        anchor_tags = {t.lower() for t in anchor.tags}
        out = []
        for candidate in self.products:
            if candidate.product_id == anchor.product_id:
                continue
            same_brand = bool(anchor.brand and candidate.brand
                              and anchor.brand.lower() == candidate.brand.lower())
            shared = anchor_tags & {t.lower() for t in candidate.tags}
            if not same_brand and not shared:
                continue
            strength = (0.6 if same_brand else 0.0) + min(0.4, 0.2 * len(shared))
            reason = (f"More from {candidate.brand}" if same_brand
                      else f"Similar: {', '.join(sorted(shared)[:2])}")
            out.append(Recommendation(
                product=candidate,
                score=WEIGHT_AFFINITY * strength,
                reason=reason,
                kind="affinity",
            ))
        return out

    # ---------------- blend ----------------

    def recommend(self, anchor_id: str, k: int = 6,
                  min_score: float = 0.25) -> list[Recommendation]:
        """Top-k recommendations for one product.

        `min_score` is a floor, not a suggestion: it is better to return three
        good items than to pad to k with weak ones. The caller asked for relevant
        recommendations, and padding is what makes a recommender feel broken.
        """
        anchor = self.by_id.get(anchor_id)
        if anchor is None:
            return []

        # Best score per candidate, keeping the reason that earned it. A product
        # can be both a complement and same-brand; it should appear once, scored
        # by its strongest justification.
        best: dict[str, Recommendation] = {}
        for rec in (self._complements(anchor) + self._substitutes(anchor)
                    + self._affinity(anchor)):
            cooc = self.score_from_cooccurrence(anchor_id, rec.product.product_id)
            if cooc:
                rec = Recommendation(
                    product=rec.product,
                    score=rec.score + WEIGHT_COOCCURRENCE * cooc,
                    reason="Customers who bought this also bought this",
                    kind="cooccurrence",
                )
            existing = best.get(rec.product.product_id)
            if existing is None or rec.score > existing.score:
                best[rec.product.product_id] = rec

        ranked = sorted(best.values(), key=lambda r: -r.score)
        kept = [r for r in ranked if r.score >= min_score]

        # Diversity: cap any single category at ~half the slots, so a query for
        # one watch does not return six watches. Complements are cross-category
        # by construction, so this mostly limits substitutes.
        cap = max(1, k // 2)
        counts: dict[str, int] = {}
        out: list[Recommendation] = []
        for rec in kept:
            category = rec.product.category or "?"
            if counts.get(category, 0) >= cap and len(out) >= cap:
                continue
            counts[category] = counts.get(category, 0) + 1
            out.append(rec)
            if len(out) >= k:
                break

        log.debug("recommendations built",
                  extra={"anchor": anchor_id, "candidates": len(best),
                         "returned": len(out)})
        return out

    def recommend_for_titles(self, titles: list[str], k: int = 6) -> list[Recommendation]:
        """Recommendations for a resolved cluster, matched back by title.

        The ranking pipeline works in Listings, which have titles but not
        catalogue ids. This maps back so the API can serve recommendations for a
        search result without threading product ids through every stage.
        """
        wanted = {t.strip().lower() for t in titles}
        for product in self.products:
            if product.title.strip().lower() in wanted:
                return self.recommend(product.product_id, k=k)
        return []


_active: Recommender | None = None


def get_recommender(products: list[Product] | None = None) -> Recommender:
    """Process-wide recommender over the active catalogue."""
    global _active
    if _active is not None and products is None:
        return _active
    if products is None:
        from .catalogue import get_catalogue

        products = get_catalogue().all_products()
    recommender = Recommender(products)
    if products is not None:
        _active = recommender
    return recommender


def reset_recommender() -> None:
    global _active
    _active = None
