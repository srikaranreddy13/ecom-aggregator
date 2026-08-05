"""
Synthetic marketplace with a moving price.

WHY THIS EXISTS -- read this before dismissing it as a toy.

`MockAdapter` reads a static JSON file, so it returns the identical price every
time it is called. `scripts/collect_prices.py` was therefore writing the same
number into `price_history` day after day: 30 rows across 2 days, all flat. You
could have run that collector for six months and the forecasting module would
have had a perfectly horizontal series to train on. The price-history table
looked healthy and was worthless.

This adapter fixes that by modelling how a real listing price actually moves:

  * a TWO-FACTOR price walk (see `price_on`): a market-wide component shared by
    every seller of a product, plus a small seller-specific deviation. This
    structure is the whole trick -- see the note below.
  * a slow downward trend -- electronics depreciate over a product's life
  * weekly seasonality -- weekend promotions
  * occasional sale events, entered and exited with persistence rather than
    flipping independently each day
  * per-seller behaviour: one source undercuts, another is premium
  * stochastic stock-outs, so a listing can vanish and reappear the way a real
    feed does

WHY TWO FACTORS AND NOT ONE PER SELLER. The first version of this file gave each
seller a fully independent random walk. A test caught the consequence: after
180 days the walks had diverged by up to 35%, which completely swamped the ~10%
spread between the seller archetypes, so the discount seller was routinely more
expensive than the premium one. Seller ordering was pure noise.

That is wrong as a model, not merely as a number. Competing sellers of the same
product face the same wholesale cost and the same demand, so their prices move
together; what differs is a persistent margin plus a small idiosyncratic wobble.
Modelling that as independent walks also destroys the MCDM demonstration -- if
"cheap seller" is random, a price-weighted ranking is ranking noise.

So the walk decomposes: a market factor keyed on (product, day) that every
seller shares, and a tightly-bounded seller factor keyed on (seller, product,
day). Cross-seller correlation is high, archetype ordering survives, and prices
still move enough to be worth forecasting.

DETERMINISM IS THE KEY PROPERTY. Every value is derived from
hash(seed, item_id, date) rather than a live RNG. That means:

  * asking for 2026-06-01 today and again next week returns the same price
  * a 90-day history can be BACKFILLED (`scripts/backfill_prices.py`), so the
    forecasting module is developable today instead of in 30 days
  * tests are reproducible, and so are figures in the paper

HONESTY REQUIREMENT: rows written by this adapter are tagged with source names
prefixed `sim_`, and `is_synthetic` is True. Any evaluation must report which
rows are simulated. Simulated data validates that the forecasting *pipeline*
works; it cannot validate that the model predicts the real world. Do not let
those two claims blur together in the write-up.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, timedelta

from ..config import settings
from ..logging_setup import get_logger
from .base import Listing, SourceAdapter

log = get_logger(__name__)


# Seller archetypes.
#
# THE CRITERIA MUST GENUINELY CONFLICT, or the MCDM module has nothing to
# decide. An early version gave the premium seller both the fastest delivery and
# the best rating, so two of the three shopper profiles picked the same seller
# and the weight vector looked half-dead. That was an artefact of the fixture,
# not of TOPSIS.
#
# Each archetype below wins exactly one criterion, and each is a pattern you can
# point at in a real marketplace:
#
#   sim_discount  cheapest, mediocre feedback, unhurried   -> wins PRICE
#   sim_warehouse big reputable store, economy shipping    -> wins RATING
#   sim_express   pays for speed, charges for it            -> wins DELIVERY
#   sim_standard  the unremarkable middle                   -> wins nothing
#
# Nobody wins all three. That is the point: there is no single "best" offer, so
# the user's weights have to decide -- which is the project's core argument.
SELLER_PROFILES = {
    "sim_discount": {
        "price_multiplier": 0.930,
        "delivery_days": (4, 7),
        "seller_rating": 4.2,
        "seller_name": "ValueBazaar",
        "volatility_scale": 1.35,     # discounters reprice restlessly
        "sale_probability": 0.10,
        "stockout_probability": 0.06,
    },
    "sim_warehouse": {
        "price_multiplier": 0.972,
        "delivery_days": (6, 9),      # economy shipping is the trade-off
        "seller_rating": 4.9,         # long-established, excellent feedback
        "seller_name": "GreatDealsWarehouse",
        "volatility_scale": 0.9,
        "sale_probability": 0.07,
        "stockout_probability": 0.04,
    },
    "sim_standard": {
        "price_multiplier": 1.0,
        "delivery_days": (2, 4),
        "seller_rating": 4.5,
        "seller_name": "MetroRetail",
        "volatility_scale": 1.0,
        "sale_probability": 0.06,
        "stockout_probability": 0.03,
    },
    "sim_express": {
        "price_multiplier": 1.058,
        "delivery_days": (1, 2),      # next-day, and you pay for it
        "seller_rating": 4.6,
        "seller_name": "PrimeDirect",
        "volatility_scale": 0.6,      # premium sellers hold price
        "sale_probability": 0.03,
        "stockout_probability": 0.01,
    },
}


def _unit_random(*parts: object) -> float:
    """Deterministic float in [0, 1) from any set of components.

    Uses blake2b rather than Python's hash(), which is salted per process and
    would make results differ between runs -- destroying the whole point.
    """
    payload = "|".join(str(p) for p in parts).encode("utf-8")
    digest = hashlib.blake2b(payload, digest_size=8).digest()
    return int.from_bytes(digest, "big") / float(1 << 64)


def _gauss(*parts: object) -> float:
    """Deterministic standard-normal draw (Box-Muller on two unit randoms)."""
    u1 = max(_unit_random("g1", *parts), 1e-12)
    u2 = _unit_random("g2", *parts)
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


class SyntheticMarketAdapter(SourceAdapter):
    """A single simulated seller. Instantiate one per profile."""

    is_synthetic = True

    def __init__(
        self,
        profile: str = "sim_standard",
        catalogue_path=None,
        seed: int | None = None,
        as_of: date | None = None,
        volatility: float | None = None,
        products=None,
    ):
        """One simulated seller.

        `products` is a list of `src.catalogue.Product`. When omitted the
        process-wide catalogue is used -- which is the LIVE one by default, so
        this seller offers all 194 products across 24 categories rather than a
        handful of hardcoded phones.

        That split is the point: the product dimension comes from a real remote
        catalogue, and this class only supplies the SELLER dimension -- price,
        delivery, rating, availability. Swap the catalogue and the same seller
        logic covers clothing, groceries and furniture with no change here.
        """
        if profile not in SELLER_PROFILES:
            raise ValueError(
                f"Unknown profile {profile!r}. Choose from {sorted(SELLER_PROFILES)}"
            )
        self.name = profile
        self.profile = SELLER_PROFILES[profile]
        self.seed = settings.synthetic_seed if seed is None else seed
        self.as_of = as_of or date.today()
        self.volatility = (
            settings.synthetic_daily_volatility if volatility is None else volatility
        )

        if products is None:
            from ..catalogue import FileCatalogue, get_catalogue

            source = (FileCatalogue(catalogue_path) if catalogue_path
                      else get_catalogue())
            products = source.all_products()

        # Keyed by product id: this adapter IS one seller, so it lists each
        # product exactly once.
        self._products = {p.product_id: p for p in products}

    # ------------------------------------------------------------ price model

    def _base_price(self, product_id: str, anchor: float) -> float:
        """Launch price for this product at this seller.

        Jitter is +/-0.5% only. It exists so two sellers do not price a product
        to the identical rupee, and it is kept inside the deviation budget
        documented on MAX_SELLER_DEVIATION.
        """
        jitter = 1.0 + 0.01 * (_unit_random(self.seed, "base", self.name, product_id) - 0.5)
        return anchor * self.profile["price_multiplier"] * jitter

    def _sale_active(self, product_id: str, day: date) -> bool:
        """Sale events persist for a few days instead of flickering daily.

        Bucketing by 3-day window is what gives runs of discounted days -- an
        independent coin flip per day produces a sawtooth no real feed shows.
        """
        window = day.toordinal() // 3
        p = _unit_random(self.seed, "sale", self.name, product_id, window)
        return p < self.profile["sale_probability"]

    # Epoch for the cumulative walk. Any fixed date works; it only has to be
    # stable, because moving it changes every historical price.
    EPOCH = date(2026, 1, 1)

    # Trailing window of shocks that still contribute. A finite window keeps the
    # series bounded (a true unbounded walk eventually drifts to absurd prices)
    # and keeps the cost of a single price O(1) rather than O(product age).
    WALK_WINDOW_DAYS = 180

    # Total seller-specific deviation budget. Sized against the archetype gaps:
    # the narrowest gap that MUST hold is discount -> warehouse (0.930 -> 0.972,
    # i.e. 4.5%), so the per-side budget has to stay under ~2.25%. This cap plus
    # the +/-0.5% base jitter comes to ~2.0%, which keeps sim_discount reliably
    # the cheapest and sim_express reliably the dearest.
    #
    # The two MIDDLE archetypes can and do swap places. That is left alone on
    # purpose -- mid-market sellers really do trade positions week to week, and
    # nothing in the project depends on their relative order.
    MAX_SELLER_DEVIATION = 0.015

    def _market_walk(self, product_id: str, age_days: int) -> float:
        """Market-wide log-price deviation. Shared by every seller.

        Deliberately NOT keyed on self.name -- that is what makes competing
        sellers move together.
        """
        walk = 0.0
        for d in range(max(0, age_days - self.WALK_WINDOW_DAYS), age_days):
            walk += self.volatility * _gauss(self.seed, "market", product_id, d)
        return max(-0.30, min(0.30, walk))

    def _seller_walk(self, product_id: str, age_days: int) -> float:
        """Small seller-specific deviation on top of the market factor.

        Scaled by the archetype's `volatility_scale`, so a discounter reprices
        more restlessly than a premium seller -- but never enough to invert the
        margin ordering.
        """
        sigma = self.volatility * 0.35 * self.profile["volatility_scale"]
        walk = 0.0
        for d in range(max(0, age_days - 30), age_days):
            walk += sigma * _gauss(self.seed, "seller", self.name, product_id, d)
        return max(-self.MAX_SELLER_DEVIATION,
                   min(self.MAX_SELLER_DEVIATION, walk))

    def price_on(self, product_id: str, anchor: float, day: date,
                 include_sales: bool = True) -> float:
        """Deterministic price for one product, one seller, one day.

        `include_sales=False` returns the underlying price with promotions
        stripped -- useful for asserting the archetype ordering, which a deep
        enough sale is *supposed* to be able to invert.
        """
        base = self._base_price(product_id, anchor)
        age_days = (day - self.EPOCH).days

        # 1. depreciation -- ~6% a year, compounded daily
        trend = math.exp(-0.06 / 365.0 * max(0, age_days))

        # 2. two-factor walk (see the module docstring for why)
        walk = self._market_walk(product_id, age_days) + \
            self._seller_walk(product_id, age_days)

        # 3. weekly seasonality -- weekend promotions
        weekly = 1.0 - 0.008 * math.sin(2 * math.pi * day.weekday() / 7.0)

        # 4. sale event
        sale = 1.0
        if include_sales and self._sale_active(product_id, day):
            depth = 0.05 + 0.12 * _unit_random(self.seed, "depth", self.name,
                                               product_id, day.toordinal() // 3)
            sale = 1.0 - depth

        price = base * trend * math.exp(walk) * weekly * sale
        return round(price, 2)

    def in_stock(self, product_id: str, day: date) -> bool:
        p = _unit_random(self.seed, "stock", self.name, product_id, day.isoformat())
        return p >= self.profile["stockout_probability"]

    def _delivery_days(self, product_id: str, day: date) -> int:
        lo, hi = self.profile["delivery_days"]
        r = _unit_random(self.seed, "ship", self.name, product_id, day.isoformat())
        return lo + int(r * (hi - lo + 1))

    def _rating(self, product_id: str) -> float:
        """Per-product feedback around the seller's baseline.

        Drift is kept narrow (+/-0.05) on purpose: rating is the criterion that
        separates the archetypes, so wide noise here would blur the very
        distinction the MCDM demo depends on.
        """
        base = self.profile["seller_rating"]
        drift = 0.1 * (_unit_random(self.seed, "rate", self.name, product_id) - 0.5)
        return round(min(5.0, max(1.0, base + drift)), 1)

    # ------------------------------------------------------------ adapter API

    def listings_on(self, day: date, query: str = "", limit: int = 25,
                    category: str | None = None) -> list[Listing]:
        """All listings this seller offers on a given day. The time machine."""
        q = query.lower().strip()
        tokens = q.split()
        wanted_category = category.strip().lower() if category else None

        out: list[Listing] = []
        for product_id, product in self._products.items():
            if wanted_category and (product.category or "").lower() != wanted_category:
                continue
            if tokens:
                # Match across title, brand, category and tags -- not just the
                # title. "shirt" has to find "Blue & Black Check Shirt", and
                # "apple" has to find products whose brand is Apple but whose
                # title never says so.
                haystack = " ".join(filter(None, [
                    product.title, product.brand, product.category,
                    " ".join(product.tags),
                ])).lower()
                if not all(tok in haystack for tok in tokens):
                    continue
            if not self.in_stock(product_id, day):
                continue

            out.append(
                Listing(
                    source=self.name,
                    # Stable per (seller, product) so a price series joins across days.
                    source_item_id=f"{self.name[4:]}-{product_id}",
                    title=product.title,
                    price=self.price_on(product_id, product.reference_price, day),
                    currency=product.currency,
                    delivery_days=self._delivery_days(product_id, day),
                    seller_rating=self._rating(product_id),
                    seller_name=self.profile["seller_name"],
                    brand=product.brand,
                    image_url=product.image_url,
                    raw={
                        "synthetic": True,
                        "observed_on": day.isoformat(),
                        "product_id": product_id,
                        "category": product.category,
                        "anchor_price": product.reference_price,
                        "on_sale": self._sale_active(product_id, day),
                        "catalogue_rating": product.rating,
                    },
                )
            )
            if len(out) >= limit:
                break
        return out

    def search(self, query: str, limit: int = 25,
               category: str | None = None) -> list[Listing]:
        return self.listings_on(self.as_of, query, limit, category=category)

    def health(self) -> bool:
        return bool(self._products)

    @property
    def categories(self) -> list[str]:
        return sorted({p.category for p in self._products.values() if p.category})


def synthetic_market(
    seed: int | None = None,
    as_of: date | None = None,
    products=None,
) -> list[SyntheticMarketAdapter]:
    """The full simulated market: one adapter per seller archetype.

    The catalogue is loaded ONCE and shared across all four sellers. Letting each
    construct its own would fetch the live catalogue four times per process --
    four times the latency and four times the load on a free public API for
    identical data.
    """
    if products is None:
        from ..catalogue import get_catalogue

        products = get_catalogue().all_products()

    return [
        SyntheticMarketAdapter(profile=p, seed=seed, as_of=as_of, products=products)
        for p in SELLER_PROFILES
    ]


def date_range(days: int, end: date | None = None) -> list[date]:
    """The last `days` dates ending at `end` (inclusive). For backfill loops."""
    end = end or date.today()
    return [end - timedelta(days=i) for i in range(days - 1, -1, -1)]
