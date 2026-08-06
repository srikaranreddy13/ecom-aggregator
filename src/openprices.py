"""
Open Food Facts / Open Prices: REAL products at REAL prices in REAL shops.

WHY THIS MODULE EXISTS. Every other data path in this project is either simulated
(the synthetic seller layer) or gated behind an approval queue (eBay, whose
registration was rejected; Amazon, whose API needs a qualifying Associates
account). This one is neither:

    * no API key
    * no registration
    * no approval wait
    * genuinely real data

Open Food Facts is a public product database (~4.4M items with prices attached).
Open Prices is its crowd-sourced price layer: 286,000+ observations, each tied to
a real shop with a real name and city, and each carrying the date it was seen.

WHAT THAT UNLOCKS THAT NOTHING ELSE HERE DID:

  1. Real multi-seller price comparison. Nutella has 161 price records across
     dozens of named shops -- Auchan Grenoble, ALDI, Biocoop, Naturalia. That is
     the aggregator's actual job, performed on data nobody invented.
  2. Real price HISTORY. Every observation is dated, so `price_history` can be
     populated with genuine market prices instead of simulated ones. This is the
     first thing in the project that makes `real_observations` on /health non-zero.

HONEST LIMITS, and they are real:

  * Groceries and household goods only. No phones, no laptops. Open Food Facts is
    a food database; the household range comes from Open Beauty/Products Facts.
  * Mostly European shops, priced in EUR. Converted to the base currency by
    src/money.py, so a stale FX snapshot shifts every price -- see /health.
  * Crowd-sourced, so coverage is uneven and a price may be months old. The
    observation date is carried through and shown rather than hidden.
  * Physical shops have no delivery time and no seller rating. Those criteria are
    left as None and imputed equally by the ranking stage, which means for this
    source the MCDM ranks on price alone. That is the correct answer for a
    supermarket shelf, not a shortcoming to paper over -- but it does mean the
    three-profile demo is less interesting here than on the simulated market.

So this source is the right choice when the question is "are the prices real?",
and the synthetic market is the right choice when the question is "does
multi-criteria ranking work?". Both are wired; neither pretends to be the other.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .adapters import http
from .adapters.base import Listing, SourceAdapter
from .adapters.errors import AdapterError
from .catalogue import Catalogue, Product
from .logging_setup import get_logger

log = get_logger(__name__)

PRICES_BASE = "https://prices.openfoodfacts.org/api/v1"
OFF_BASE = "https://world.openfoodfacts.org/api/v2"

# Open Food Facts asks API clients to identify themselves. Doing so is a
# condition of polite use, not decoration.
USER_AGENT = "ecom-aggregator/0.2 (academic research project)"


@dataclass(frozen=True)
class ShopPrice:
    """One observation: a price, in a shop, on a date."""

    price: float
    currency: str
    location_id: int
    shop_name: str
    city: str | None
    country: str | None
    observed_on: str

    @property
    def source_key(self) -> str:
        """Stable per-shop source name, e.g. 'shop:aldi-saint-flour'."""
        slug = "".join(
            ch if ch.isalnum() else "-"
            for ch in f"{self.shop_name} {self.city or ''}".strip().lower()
        )
        while "--" in slug:
            slug = slug.replace("--", "-")
        return f"shop:{slug.strip('-')[:48]}"


class _OpenPricesClient:
    """Shared HTTP access plus a shop-name cache.

    Location names arrive by id on every price record, so a naive implementation
    issues one extra request per price -- 161 requests to show one product. The
    cache makes it one request per distinct shop, once per process.
    """

    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        self._locations: dict[int, dict] = {}

    def _get(self, url: str, **params):
        resp = http.get(url, source="openprices", params=params,
                        timeout=self.timeout,
                        headers={"User-Agent": USER_AGENT})
        return http.json_body(resp, source="openprices")

    def search_products(self, query: str, limit: int = 40) -> list[dict]:
        """Products that have at least one price, matching `query` by name.

        Ordered by price_count descending: a product with 161 observations
        demonstrates comparison, one with a single price cannot.
        """
        params = {"size": min(limit, 100), "order_by": "-price_count"}
        if query.strip():
            params["product_name__like"] = query.strip()
        payload = self._get(f"{PRICES_BASE}/products", **params)
        return payload.get("items") or []

    def location(self, location_id: int) -> dict:
        if location_id in self._locations:
            return self._locations[location_id]
        try:
            data = self._get(f"{PRICES_BASE}/locations/{location_id}")
        except AdapterError as exc:
            log.debug("location lookup failed",
                      extra={"location_id": location_id, "error": str(exc)})
            data = {}
        self._locations[location_id] = data
        return data

    def prices_for(self, code: str, limit: int = 40) -> list[ShopPrice]:
        payload = self._get(f"{PRICES_BASE}/prices", product_code=code,
                            size=min(limit, 100), order_by="-date")
        out: list[ShopPrice] = []
        for row in payload.get("items") or []:
            price = row.get("price")
            if price is None:
                continue
            loc_id = row.get("location_id")
            loc = self.location(loc_id) if loc_id else {}
            out.append(ShopPrice(
                price=float(price),
                currency=(row.get("currency") or "EUR").upper(),
                location_id=loc_id or 0,
                shop_name=loc.get("osm_name") or f"shop {loc_id}",
                city=loc.get("osm_address_city"),
                country=loc.get("osm_address_country"),
                observed_on=str(row.get("date") or "")[:10],
            ))
        return out


_client = _OpenPricesClient()


# ---------------------------------------------------------------- catalogue


class OpenPricesCatalogue(Catalogue):
    """Real products that have at least one real price observation."""

    name = "openprices"
    is_live = True

    def __init__(self, seed_queries: list[str] | None = None, per_query: int = 30):
        # There is no way to enumerate 4.4M products, and no reason to: the
        # catalogue only needs enough breadth to browse. A handful of common
        # grocery terms gives a realistic shelf without a huge cold-start fetch.
        self.seed_queries = seed_queries or [
            "", "milk", "chocolate", "coffee", "pasta", "rice", "shampoo",
            "juice", "cheese", "yogurt", "bread", "oil",
        ]
        self.per_query = per_query
        self._products: list[Product] | None = None

    def _to_product(self, row: dict) -> Product | None:
        code = row.get("code")
        title = (row.get("product_name") or "").strip()
        if not code or not title:
            return None

        brands = (row.get("brands") or "").split(",")
        brand = brands[0].strip() or None

        # categories_tags look like "en:hazelnut-spreads"; keep the readable half.
        tags = []
        for tag in (row.get("categories_tags") or [])[:6]:
            tags.append(tag.split(":", 1)[-1].replace("-", " "))

        quantity = (row.get("quantity") or "").strip()
        display = f"{title} {quantity}".strip() if quantity else title

        return Product(
            product_id=f"op-{code}",
            title=display,
            brand=brand,
            category=(tags[0] if tags else "groceries"),
            # A catalogue reference price is not needed here: real shop prices
            # arrive from the adapter. Zero marks "no reference price known".
            reference_price=0.0,
            currency="EUR",
            image_url=row.get("image_url"),
            description=None,
            rating=None,
            tags=tags,
            raw=row,
            is_reference_price=False,   # prices for this source are REAL
        )

    def all_products(self) -> list[Product]:
        if self._products is not None:
            return self._products

        started = time.monotonic()
        seen: dict[str, Product] = {}
        for query in self.seed_queries:
            try:
                for row in _client.search_products(query, limit=self.per_query):
                    product = self._to_product(row)
                    if product and product.product_id not in seen:
                        seen[product.product_id] = product
            except AdapterError as exc:
                log.warning("open prices seed query failed",
                            extra={"query": query, "error": str(exc)})

        self._products = list(seen.values())
        log.info("catalogue loaded",
                 extra={"source": self.name, "products": len(self._products),
                        "fetch_ms": round((time.monotonic() - started) * 1000, 1)})
        return self._products

    def search(self, query: str, limit: int = 60,
               category: str | None = None) -> list[Product]:
        """Query the API directly rather than filtering a cached page.

        The remote set is 4.4M products; a local filter over whatever happened to
        be cached would make most searches return nothing and look broken.
        """
        if not query.strip():
            return super().search(query, limit=limit, category=category)
        try:
            rows = _client.search_products(query, limit=limit)
        except AdapterError as exc:
            log.warning("open prices search failed, using cached products",
                        extra={"query": query, "error": str(exc)})
            return super().search(query, limit=limit, category=category)

        out = []
        for row in rows:
            product = self._to_product(row)
            if product:
                out.append(product)
        return out[:limit]


# ---------------------------------------------------------------- adapter


class OpenPricesAdapter(SourceAdapter):
    """Real shop offers for real products.

    Unlike every other adapter here, ONE instance represents MANY sellers: each
    price observation becomes a Listing whose `source` is the shop it was seen in.
    That is what makes this genuine price comparison rather than a simulation of
    one.
    """

    name = "openprices"
    is_synthetic = False
    cacheable = True

    def __init__(self, catalogue: OpenPricesCatalogue | None = None,
                 max_products: int = 8, prices_per_product: int = 12):
        self.catalogue = catalogue or OpenPricesCatalogue()
        # Each product costs one prices request plus a lookup per new shop, so
        # breadth is capped: a query matching 700 products must not fan out into
        # hundreds of calls against a free community API.
        self.max_products = max_products
        self.prices_per_product = prices_per_product

    def search(self, query: str, limit: int = 25,
               category: str | None = None) -> list[Listing]:
        products = self.catalogue.search(query, limit=self.max_products,
                                        category=category)
        out: list[Listing] = []

        for product in products[:self.max_products]:
            code = product.product_id.removeprefix("op-")
            try:
                prices = _client.prices_for(code, limit=self.prices_per_product)
            except AdapterError as exc:
                log.warning("price lookup failed",
                            extra={"code": code, "error": str(exc)})
                continue

            for shop in prices:
                out.append(Listing(
                    source=shop.source_key,
                    # One observation per shop per day. Including the date keeps
                    # repeated visits to one shop as separate history points
                    # rather than silently collapsing them.
                    source_item_id=f"{code}-{shop.location_id}-{shop.observed_on}",
                    title=product.title,
                    price=shop.price,
                    currency=shop.currency,
                    # A supermarket shelf has no delivery estimate and no seller
                    # rating. Left as None and imputed equally downstream, so the
                    # ranking for this source is driven by price -- which is the
                    # honest answer, not a gap to fill with invented numbers.
                    delivery_days=None,
                    seller_rating=None,
                    seller_name=f"{shop.shop_name}"
                                + (f", {shop.city}" if shop.city else ""),
                    url=f"https://prices.openfoodfacts.org/products/{code}",
                    image_url=product.image_url,
                    brand=product.brand,
                    raw={
                        "synthetic": False,
                        "observed_on": shop.observed_on,
                        "product_id": product.product_id,
                        "category": product.category,
                        "shop_city": shop.city,
                        "shop_country": shop.country,
                        "real_price": True,
                    },
                ))
                if len(out) >= limit:
                    return out
        return out

    def health(self) -> bool:
        try:
            _client.search_products("", limit=1)
            return True
        except Exception:
            return False
