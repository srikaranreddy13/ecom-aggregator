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

from .catalogue import COMPLEMENT_CATEGORIES, Product, get_catalogue
from .logging_setup import get_logger
from .matching.attributes import Attributes

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

# A complement sourced from the bundled catalogue, rather than seen priced in
# this actual search, is scored slightly below an equivalent live one. It is
# still a genuinely useful suggestion -- a phone case really does go with a
# phone -- but it is a category-level idea, not a quote, and the score should
# reflect that it is the weaker of the two kinds of "complement".
CATALOGUE_COMPLEMENT_DAMPING = 0.8
# At most this many catalogue suggestions per complementary category, so one
# recommendation panel cannot fill up with eight variations of "phone case".
MAX_CATALOGUE_COMPLEMENTS_PER_CATEGORY = 3


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
        """Complement candidates, from whichever source can actually supply one.

        Two sources, and they are not interchangeable:

          anchor.complements   explicit links, built by catalogue._link_complements.
                                Only ever populated when this Recommender was built
                                from the full catalogue, because only there does a
                                laptop sit in the same product list as a bag.

          category-pair lookup Fallback for everything else -- in particular a
                                Recommender built by from_search_results over one
                                live query's results. Those results are, by
                                construction, ONE product type (a phone search
                                returns phones), so there is no bag inside them
                                to recommend as a genuine complement. The fallback
                                reaches into the bundled catalogue instead, and is
                                scored and labelled as what it is: a category-level
                                suggestion, not a product this search actually saw
                                priced. See Product.is_reference_price.
        """
        if anchor.complements:
            return self._explicit_complements(anchor)
        if anchor.category:
            return self._category_pair_complements(anchor)
        return []

    def _explicit_complements(self, anchor: Product) -> list[Recommendation]:
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

    def _category_pair_complements(self, anchor: Product) -> list[Recommendation]:
        """No price-ratio filter here, deliberately, unlike _explicit_complements.

        The anchor is a live product (price in the pipeline's base currency,
        e.g. INR); the bundled catalogue's reference prices are in USD. A ratio
        between them without conversion would be exactly the silent
        wrong-currency-comparison bug this project has been bitten by more than
        once (see money.py). Converting currency for a soft, best-effort
        suggestion is not worth the complexity it would add here, so the
        filter is left out rather than built unsafely -- is_reference_price on
        every result it returns is what tells the caller these are category
        ideas, not priced, comparable offers.
        """
        wanted = COMPLEMENT_CATEGORIES.get(anchor.category or "", [])
        if not wanted:
            return []

        try:
            catalogue_products = get_catalogue().all_products()
        except Exception as exc:  # noqa: BLE001 -- a recommendation must never
            # crash a search result over the bundled catalogue being briefly
            # unreachable; worse case is simply no complement suggestions.
            log.warning("category-pair complement lookup skipped",
                        extra={"error": str(exc)[:200]})
            return []

        readable = anchor.category.replace("-", " ")
        out: list[Recommendation] = []
        per_category: dict[str, int] = {}
        for product in catalogue_products:
            if product.category not in wanted:
                continue
            if per_category.get(product.category, 0) >= MAX_CATALOGUE_COMPLEMENTS_PER_CATEGORY:
                continue
            per_category[product.category] = per_category.get(product.category, 0) + 1
            quality = (product.rating or 3.0) / 5.0
            out.append(Recommendation(
                product=product,
                score=WEIGHT_COMPLEMENT * quality * CATALOGUE_COMPLEMENT_DAMPING,
                reason=f"Often paired with {readable} (catalogue suggestion, not a live offer)",
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
                  min_score: float = 0.20) -> list[Recommendation]:
        """Top-k recommendations for one product.

        `min_score` is a floor, not a suggestion: it is better to return three
        good items than to pad to k with weak ones. The caller asked for relevant
        recommendations, and padding is what makes a recommender feel broken.

        REGRESSION THIS FIXES. The floor used to be 0.25 -- but a same-brand
        match with no shared tags, which is the ordinary case for a live
        search result (tags come from a "specs" string that is frequently
        empty), scores WEIGHT_AFFINITY * 0.6 = 0.21. That is below 0.25, so
        the single most relevant thing the system could say about a product
        with no guessed category -- "more from Sony", when nothing else can
        fire at all -- was being silently discarded as if it were noise. The
        user saw "No related products found" for products that very much had
        a related product; the floor, not the signal, was broken. 0.20 is not
        an arbitrary relaxation: it is the smallest value that admits
        affinity's own documented floor case without admitting the near-zero
        scores padding was meant to exclude.
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


def from_search_results(results: list[dict]) -> Recommender:
    """Build a recommender over the products a search actually returned.

    WHY THIS EXISTS. The catalogue-backed recommender assumes the products on
    screen come from the catalogue it indexed. With a search-driven source like
    Google Shopping that is false: the screen shows whatever the retailer search
    returned, so title lookup against the catalogue never hits and every product
    reported zero recommendations.

    Recommending from the fetched result set is also the more honest answer for a
    search-driven aggregator -- it can only suggest things it has really seen
    priced, and it costs no extra API quota because the search response is
    already cached.

    `row["category"]` is left as None when the pipeline could not populate or
    guess one (see matching.product_meta.guess_category), rather than defaulted
    to a placeholder. An earlier version defaulted it to the literal string
    "search-result" for every product, which made _substitutes "work" by
    accident -- every product in one query shared the one placeholder category,
    so the bucket was really just "everything else in this search", regardless
    of whether it was actually the same kind of product. Leaving it None when
    genuinely unknown means the substitute signal goes quiet instead of lying.
    """
    products = []
    for row in results:
        offers = row.get("all_offers") or []
        cheapest = min((o.get("price", 0.0) for o in offers), default=0.0)
        products.append(Product(
            product_id=row.get("cluster_id") or row.get("product", ""),
            title=row.get("product", ""),
            brand=row.get("brand"),
            category=row.get("category"),
            reference_price=float(cheapest or 0.0),
            currency=row.get("currency", "INR"),
            image_url=row.get("image_url"),
            rating=max((o.get("seller_rating") or 0) for o in offers) if offers else None,
            # Attributes.summary() returns NO_SPECS_SENTINEL, not "", when
            # nothing was extracted -- so the naive split used to turn that
            # ONE PLACEHOLDER PHRASE into a "tag" every spec-less product
            # shared with every other spec-less product, regardless of what
            # either of them actually was. _affinity's shared-tag check then
            # scored a perfume against an unrelated t-shirt as "similar",
            # with a reason string that would have read literally
            # "Similar: no specs detected" had it ever scored high enough to
            # surface. Filtered out by name here, not by retyping the phrase,
            # so the two places can never drift apart again.
            tags=[t for t in (row.get("specs") or "").split(" · ")
                  if t and t != Attributes.NO_SPECS_SENTINEL],
            is_reference_price=False,
        ))
    return Recommender(products)


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
