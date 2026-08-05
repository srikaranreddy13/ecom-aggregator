"""
Product catalogue -- the PRODUCT dimension, separate from the SELLER dimension.

THE ARCHITECTURAL POINT, and it is the one that makes "browse anything" possible:

    an aggregator has no catalogue of its own.

Amazon has a product catalogue because Amazon *is* a retailer -- sellers list
into its taxonomy. An aggregator (Google Shopping, PriceGrabber, Junglee) has
none: it asks retailer APIs at query time and resolves whatever comes back. So
hand-maintaining a JSON list of products is not a small version of the real
thing, it is a different and unwinnable thing -- every domain a user might search
would need someone to type it in first.

This module therefore treats the product list as a REMOTE, REPLACEABLE resource
with a local fallback, never as a file to grow:

    DummyJsonCatalogue   live HTTP, no API key, 194 products across 24
                         categories -- clothing, groceries, furniture, beauty,
                         footwear, jewellery, laptops, phones -- with real
                         images. This is the default.
    FileCatalogue        data/catalogue.json. Used only when the network is
                         unavailable, so a lab machine with no internet still
                         demos. Explicitly a FALLBACK, not the product universe.

SEPARATION OF PRODUCT FROM SELLER. A catalogue entry is a product: title, brand,
category, image, a reference price. It is NOT an offer. Offers -- who sells it,
at what price, how fast, how well rated -- come from the adapter layer, because
that is the dimension the aggregator actually compares. Keeping them apart is
what lets the same product list be served by a simulated marketplace today and
by live eBay sellers tomorrow with no change here.

HONESTY. DummyJSON is a public demo API. Its products and images are real data
served over real HTTP -- the live-fetch path is genuinely exercised -- but its
prices are not quotes from a real retailer. `is_reference_price` marks that, and
anything derived from it stays labelled downstream.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from .adapters import http
from .adapters.errors import AdapterError
from .config import ROOT, settings
from .logging_setup import get_logger

log = get_logger(__name__)

DUMMYJSON_BASE = "https://dummyjson.com"
FALLBACK_PATH = ROOT / "data" / "catalogue.json"


@dataclass
class Product:
    """One distinct physical product. Not an offer."""

    product_id: str
    title: str
    brand: str | None
    category: str | None
    reference_price: float
    currency: str = "USD"
    image_url: str | None = None
    description: str | None = None
    rating: float | None = None
    tags: list[str] = field(default_factory=list)
    # Products genuinely bought alongside this one. Feeds the recommender's
    # complement rules; may be empty, in which case content similarity carries it.
    complements: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    # True when reference_price came from a demo API rather than a retailer quote.
    is_reference_price: bool = True

    def to_dict(self) -> dict:
        return {
            "product_id": self.product_id,
            "title": self.title,
            "brand": self.brand,
            "category": self.category,
            "reference_price": self.reference_price,
            "currency": self.currency,
            "image_url": self.image_url,
            "rating": self.rating,
            "tags": list(self.tags),
        }


class Catalogue:
    """Base class. A catalogue is a searchable set of products."""

    name = "unnamed"
    is_live = False

    def all_products(self) -> list[Product]:
        raise NotImplementedError

    def categories(self) -> list[str]:
        seen = {p.category for p in self.all_products() if p.category}
        return sorted(seen)

    def search(self, query: str, limit: int = 60,
               category: str | None = None) -> list[Product]:
        """Token-subset match over title, brand, category and tags.

        Deliberately generous: an aggregator that returns nothing for a slightly
        wrong query is useless, and the ranking stage downstream is what imposes
        order. Every token must appear SOMEWHERE in the product's text, so
        "blue shirt" does not match every shirt.
        """
        products = self.all_products()
        if category:
            wanted = category.strip().lower()
            products = [p for p in products
                        if (p.category or "").lower() == wanted]

        q = query.strip().lower()
        if not q:
            return products[:limit]

        tokens = [t for t in q.split() if t]
        out = []
        for p in products:
            haystack = " ".join(filter(None, [
                p.title, p.brand, p.category, p.description,
                " ".join(p.tags),
            ])).lower()
            if all(tok in haystack for tok in tokens):
                out.append(p)
        return out[:limit]


# ---------------------------------------------------------------- live


class DummyJsonCatalogue(Catalogue):
    """Live product data over HTTP, no API key required.

    194 products across 24 categories with real images. Fetched once per process
    and held, because the product dimension changes on the order of days while a
    query arrives every few seconds -- re-fetching per request would add hundreds
    of milliseconds to every search for data that has not changed.
    """

    name = "dummyjson"
    is_live = True

    # Complement rules by category pair. Category-level rather than per-product,
    # because a rule per product would need maintaining for every new product --
    # the same trap the attribute extractor avoids. "Buyers of a laptop also buy
    # a bag" holds for every laptop, present and future.
    COMPLEMENT_CATEGORIES = {
        # NOT furniture. The rule "a laptop goes with a desk and chair" is sound
        # in principle, but this catalogue's furniture is beds and sofas, so it
        # surfaced "MacBook Pro -> Annibale Colombo Bed". A rule that is right in
        # the abstract and wrong on the actual data is still wrong.
        "laptops":              ["mobile-accessories", "womens-bags"],
        "smartphones":          ["mobile-accessories", "mens-watches", "sunglasses"],
        "tablets":              ["mobile-accessories", "womens-bags"],
        "mobile-accessories":   ["smartphones", "tablets", "laptops"],
        "mens-shirts":          ["mens-shoes", "mens-watches", "sunglasses"],
        "mens-shoes":           ["mens-shirts", "sports-accessories", "mens-watches"],
        "mens-watches":         ["mens-shirts", "sunglasses", "mens-shoes"],
        "womens-dresses":       ["womens-shoes", "womens-bags", "womens-jewellery"],
        "womens-shoes":         ["womens-dresses", "womens-bags", "womens-jewellery"],
        "womens-bags":          ["womens-dresses", "womens-shoes", "sunglasses"],
        "womens-jewellery":     ["womens-dresses", "womens-watches", "womens-bags"],
        "womens-watches":       ["womens-jewellery", "womens-bags", "womens-dresses"],
        "tops":                 ["womens-shoes", "womens-bags", "womens-jewellery"],
        "beauty":               ["skin-care", "fragrances"],
        "skin-care":            ["beauty", "fragrances"],
        "fragrances":           ["beauty", "skin-care"],
        "groceries":            ["kitchen-accessories", "home-decoration"],
        "kitchen-accessories":  ["groceries", "home-decoration", "furniture"],
        "home-decoration":      ["furniture", "kitchen-accessories"],
        # kitchen-accessories removed: it produced "Sofa -> Lunch Box".
        "furniture":            ["home-decoration"],
        "sunglasses":           ["mens-watches", "womens-bags", "mens-shirts"],
        "sports-accessories":   ["mens-shoes", "mens-shirts"],
        "motorcycle":           ["vehicle", "sports-accessories"],
        "vehicle":              ["motorcycle", "sunglasses"],
    }

    def __init__(self, timeout: int | None = None, page_size: int = 100):
        self.timeout = timeout or settings.http_timeout_seconds
        self.page_size = page_size
        self._products: list[Product] | None = None
        self._fetched_at: float = 0.0

    # -------- fetch --------

    def _fetch_page(self, skip: int) -> dict:
        resp = http.get(
            f"{DUMMYJSON_BASE}/products",
            source=self.name,
            params={"limit": self.page_size, "skip": skip},
            timeout=self.timeout,
        )
        return http.json_body(resp, source=self.name)

    def _to_product(self, row: dict) -> Product:
        pid = f"dj-{row['id']}"
        return Product(
            product_id=pid,
            title=row.get("title", ""),
            brand=(row.get("brand") or None),
            category=row.get("category"),
            reference_price=float(row.get("price", 0.0)),
            currency="USD",                       # DummyJSON quotes USD
            image_url=row.get("thumbnail"),
            description=(row.get("description") or "")[:400] or None,
            rating=row.get("rating"),
            tags=list(row.get("tags") or []),
            raw=row,
            is_reference_price=True,
        )

    def _link_complements(self, products: list[Product]) -> None:
        """Attach complement product ids using the category rules.

        Picks the highest-rated few from each complementary category, so
        suggestions are plausible rather than arbitrary. Mutates in place.
        """
        by_category: dict[str, list[Product]] = {}
        for p in products:
            if p.category:
                by_category.setdefault(p.category, []).append(p)
        for bucket in by_category.values():
            bucket.sort(key=lambda p: -(p.rating or 0))

        for p in products:
            picks: list[str] = []
            for other_category in self.COMPLEMENT_CATEGORIES.get(p.category or "", []):
                for candidate in by_category.get(other_category, [])[:3]:
                    if candidate.product_id != p.product_id:
                        picks.append(candidate.product_id)
            p.complements = picks[:8]

    def all_products(self) -> list[Product]:
        if self._products is not None:
            return self._products

        started = time.monotonic()
        rows: list[dict] = []
        skip = 0
        while True:
            payload = self._fetch_page(skip)
            batch = payload.get("products") or []
            rows.extend(batch)
            total = int(payload.get("total") or len(rows))
            skip += self.page_size
            if skip >= total or not batch:
                break

        products = [self._to_product(r) for r in rows if r.get("title")]
        self._link_complements(products)
        self._products = products
        self._fetched_at = time.monotonic()

        log.info("catalogue loaded",
                 extra={"source": self.name, "products": len(products),
                        "categories": len(self.categories()),
                        "fetch_ms": round((time.monotonic() - started) * 1000, 1)})
        return products


# ---------------------------------------------------------------- fallback


class FileCatalogue(Catalogue):
    """Offline fallback from data/catalogue.json.

    Exists so the project still runs with no internet -- a real constraint on a
    lab machine during a review. It is NOT the product universe and must not be
    grown to imitate one; see the module docstring.
    """

    name = "file"
    is_live = False

    def __init__(self, path=None):
        self.path = path or FALLBACK_PATH
        self._products: list[Product] | None = None

    def all_products(self) -> list[Product]:
        if self._products is not None:
            return self._products

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        rows = payload["products"] if isinstance(payload, dict) else payload

        out = []
        for row in rows:
            attrs = row.get("attributes") or {}
            out.append(Product(
                product_id=row.get("product_id") or row.get("gold_cluster"),
                title=row.get("name") or row.get("title", ""),
                brand=row.get("brand"),
                category=row.get("category"),
                reference_price=float(row.get("mrp") or row.get("price") or 0.0),
                currency="INR",                    # the local fixture is in INR
                image_url=row.get("image_url"),
                description=row.get("search_terms"),
                rating=None,
                tags=[str(v) for v in attrs.values() if v is not None],
                complements=list(row.get("complements") or []),
                raw=row,
                is_reference_price=True,
            ))
        self._products = out
        log.info("catalogue loaded",
                 extra={"source": self.name, "products": len(out),
                        "categories": len(self.categories())})
        return out


# ---------------------------------------------------------------- selection

_active: Catalogue | None = None


def get_catalogue(prefer: str | None = None) -> Catalogue:
    """Return the process-wide catalogue, live if reachable.

    Falls back to the bundled file when the network is down. The fallback is
    logged as a WARNING rather than passing silently: serving 44 local products
    while believing you are serving 194 live ones is exactly the kind of quiet
    degradation this project has been bitten by before.
    """
    global _active
    if _active is not None and prefer is None:
        return _active

    choice = (prefer or settings.catalogue_source).lower()

    if choice in ("auto", "dummyjson", "live"):
        live = DummyJsonCatalogue()
        try:
            live.all_products()
            if prefer is None:
                _active = live
            return live
        except (AdapterError, OSError, ValueError) as exc:
            if choice != "auto":
                raise
            log.warning(
                "live catalogue unreachable, falling back to the bundled file "
                "-- product breadth is reduced",
                extra={"error": str(exc)[:200]},
            )

    fallback = FileCatalogue()
    fallback.all_products()
    if prefer is None:
        _active = fallback
    return fallback


def reset_catalogue() -> None:
    """Drop the cached catalogue. For tests."""
    global _active
    _active = None
