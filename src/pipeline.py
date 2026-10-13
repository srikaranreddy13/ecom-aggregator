"""
End-to-end orchestration: fetch -> normalise -> cache -> resolve -> rank.

Two stages were missing from the original version and are wired in here:

CACHING. `db.cache_get` / `db.cache_put` existed and were never called by
anything -- the README claimed responses were cached, and they were not. Every
repeated query re-hit the source. Now `fetch()` consults the cache per adapter,
per query, so a page refresh or a repeated evaluation run costs nothing.

CURRENCY NORMALISATION. `Listing.price` is a bare float and TOPSIS compares
those floats directly, so a USD listing from eBay would have out-ranked an INR
listing for the same product on price. Conversion now happens once, right after
fetch, before anything downstream sees a price. See src/money.py.

Failure policy: one dead source degrades the result, it never fails the query.
Auth failures additionally *disable* that adapter for the life of the pipeline,
because retrying a bad credential on every query is pure latency.
"""

from __future__ import annotations

import time

from .adapters.base import Listing, SourceAdapter
from .adapters.errors import AdapterAuthError, AdapterError
from .config import settings
from .links import build_links
from .logging_setup import get_logger
from .matching import attributes
from .matching.embedder import Embedder
from .matching.product_meta import guess_brand, guess_category
from .matching.resolver import EntityResolver, ProductCluster
from .money import count_incomparable, normalise_listings
from .ranking.mcdm import topsis
from .storage import db

log = get_logger(__name__)


class AggregatorPipeline:
    def __init__(
        self,
        adapters: list[SourceAdapter],
        resolver: EntityResolver | None = None,
        conn=None,
        cache_ttl: int | None = None,
        base_currency: str | None = None,
        record_history: bool = True,
    ):
        self.adapters = adapters
        self.resolver = resolver or EntityResolver(Embedder())
        self.conn = conn
        self.cache_ttl = settings.cache_ttl_seconds if cache_ttl is None else cache_ttl
        self.base_currency = (base_currency or settings.base_currency).upper()
        self.record_history = record_history

        # Adapters taken out of service this process (bad credentials).
        self._disabled: set[str] = set()
        self.stats: dict[str, object] = {}

    # ---------------- cache ----------------

    def _cache_key(self, adapter: SourceAdapter, query: str, limit: int,
                   category: str | None = None) -> str:
        """Cache key. MUST include every input that changes the result.

        REGRESSION THIS FIXES. The key was (adapter, limit, query) and omitted the
        category. With a search-driven source a category chip sends an EMPTY query
        plus a category, so `category=laptops` and `category=mens-shoes` produced
        the identical key -- and every category after the first was served the
        first one's cached response. Every chip showed laptops.

        The bug was invisible in shop mode, where the category was applied by the
        adapter after a query that always differed. A cache key that omits a
        parameter is a correctness bug, not a performance one.
        """
        return (f"v2:{adapter.name}:{limit}:{query.strip().lower()}"
                f":{(category or '').strip().lower()}")

    def _cached(self, adapter: SourceAdapter, query: str, limit: int,
                category: str | None = None) -> list[Listing] | None:
        if not (self.conn and settings.cache_enabled and adapter.cacheable):
            return None
        payload = db.cache_get(self.conn,
                              self._cache_key(adapter, query, limit, category),
                              ttl_seconds=self.cache_ttl)
        if payload is None:
            return None
        try:
            return [Listing.from_dict(d) for d in payload]
        except (TypeError, KeyError) as exc:
            # A schema change makes old entries unreadable. Treat it as a miss
            # rather than propagating -- a stale cache must never break a query.
            log.warning("discarding unreadable cache entry",
                        extra={"adapter": adapter.name, "error": str(exc)})
            return None

    def _store(self, adapter: SourceAdapter, query: str, limit: int,
               listings: list[Listing], category: str | None = None) -> None:
        if not (self.conn and settings.cache_enabled and adapter.cacheable):
            return
        db.cache_put(self.conn,
                     self._cache_key(adapter, query, limit, category),
                     [l.to_dict() for l in listings])

    # ---------------- fetch ----------------

    def fetch(self, query: str, limit_per_source: int = 25,
              category: str | None = None) -> list[Listing]:
        """Query every healthy adapter, normalise currency, record history."""
        started = time.monotonic()
        all_listings: list[Listing] = []
        per_source: dict[str, int] = {}
        cache_hits: list[str] = []
        failures: dict[str, str] = {}

        for ad in self.adapters:
            if ad.name in self._disabled:
                continue

            hit = self._cached(ad, query, limit_per_source, category)
            if hit is not None:
                all_listings.extend(hit)
                per_source[ad.name] = len(hit)
                cache_hits.append(ad.name)
                continue

            try:
                # Category filtering is optional on the adapter interface: a
                # live marketplace API may not expose it, and a source that
                # cannot filter should still contribute rather than be dropped.
                if category and "category" in getattr(ad.search, "__code__",
                                                     type("x", (), {})()).co_varnames:
                    got = ad.search(query, limit=limit_per_source, category=category)
                else:
                    got = ad.search(query, limit=limit_per_source)
            except AdapterAuthError as exc:
                # Not retryable and not transient: stop asking.
                self._disabled.add(ad.name)
                failures[ad.name] = str(exc)
                log.error("adapter disabled after auth failure",
                          extra={"adapter": ad.name, "error": str(exc)})
                continue
            except AdapterError as exc:
                failures[ad.name] = str(exc)
                log.warning("adapter failed, continuing with remaining sources",
                            extra={"adapter": ad.name, "error": str(exc)})
                continue
            except Exception as exc:  # noqa: BLE001 -- an adapter must never kill a query
                failures[ad.name] = str(exc)
                log.exception("adapter raised an untyped error",
                              extra={"adapter": ad.name})
                continue

            self._store(ad, query, limit_per_source, got, category)
            all_listings.extend(got)
            per_source[ad.name] = len(got)

        # Currency first: history and ranking must both be in one currency.
        all_listings = normalise_listings(all_listings, to=self.base_currency)

        if self.conn and self.record_history:
            db.record_prices(self.conn, all_listings)

        self.stats = {
            "query": query,
            "listings": len(all_listings),
            "per_source": per_source,
            "cache_hits": cache_hits,
            "failures": failures,
            "disabled_adapters": sorted(self._disabled),
            "base_currency": self.base_currency,
            # Non-zero means a missing FX rate has made some prices
            # incomparable, so the ranking for those is unreliable. A wrong
            # ranking from a missing rate looks exactly like a correct one, so
            # it has to be reported rather than merely logged.
            "prices_not_comparable": count_incomparable(all_listings),
            "synthetic_sources": sorted(
                ad.name for ad in self.adapters if getattr(ad, "is_synthetic", False)
            ),
            "fetch_ms": round((time.monotonic() - started) * 1000, 1),
        }
        log.info("fetch complete", extra=self.stats)
        return all_listings

    # ---------------- resolve ----------------

    def resolve(self, listings: list[Listing]) -> list[ProductCluster]:
        started = time.monotonic()
        clusters = self.resolver.resolve(listings)
        log.info("entity resolution complete",
                 extra={"listings": len(listings), "clusters": len(clusters),
                        "backend": self.resolver.embedder.backend,
                        "threshold": self.resolver.similarity_threshold,
                        "resolve_ms": round((time.monotonic() - started) * 1000, 1)})
        return clusters

    # ---------------- full run ----------------

    def search(
        self,
        query: str,
        weights: dict[str, float] | None = None,
        limit_per_source: int = 25,
        normalisation: str = "minmax",
        category: str | None = None,
    ) -> list[dict]:
        listings = self.fetch(query, limit_per_source, category=category)
        clusters = self.resolve(listings)

        # The three shopper profiles, evaluated for every product alongside the
        # user's own weights. The UI shows all three at once because the whole
        # argument of the project is that they disagree -- making the user click
        # three times to discover that buries the finding.
        PROFILES = {
            "budget": {"price": 0.80, "delivery_days": 0.10, "seller_rating": 0.10},
            "hurry": {"price": 0.20, "delivery_days": 0.65, "seller_rating": 0.15},
            "trust": {"price": 0.20, "delivery_days": 0.15, "seller_rating": 0.65},
        }

        results = []
        for c in clusters:
            ranked = topsis(c.listings, weights, normalisation=normalisation)
            best = ranked[0]

            profile_winners = {}
            for name, profile_weights in PROFILES.items():
                top = topsis(c.listings, profile_weights,
                             normalisation=normalisation)[0]
                profile_winners[name] = {
                    "source": top.listing.source,
                    "seller_name": top.listing.seller_name,
                    "price": top.listing.price,
                    "delivery_days": top.listing.delivery_days,
                    "seller_rating": top.listing.seller_rating,
                    "score": top.score,
                }

            first = c.listings[0]
            # Catalogue-backed sources set brand/category on the Listing itself.
            # A search-driven source (SerpApi) never does -- Google Shopping
            # returns a title and a price, nothing structured -- so brand and
            # category are guessed from the canonical title as a fallback. This
            # is what re-lights the recommender's affinity (needs brand) and
            # substitute (needs category) signals on live data; see
            # src/matching/product_meta.py for why a guess returns None rather
            # than something wrong when it is not confident.
            brand = first.brand or guess_brand(c.canonical_title)
            category = (first.raw or {}).get("category") or guess_category(c.canonical_title)
            results.append(
                {
                    "cluster_id": c.cluster_id,
                    "product": c.canonical_title,
                    "num_offers": len(c),
                    "sources": sorted({l.source for l in c.listings}),
                    "currency": self.base_currency,
                    "image_url": next(
                        (l.image_url for l in c.listings if l.image_url), None),
                    "brand": brand,
                    "category": category,
                    "specs": attributes.extract(
                        c.canonical_title, brand).summary(),
                    "profiles": profile_winners,
                    "profiles_agree": len(
                        {p["source"] for p in profile_winners.values()}) == 1,
                    "links": build_links(
                        c.canonical_title, c.listings,
                        brand=first.brand, currency=self.base_currency),
                    "price_range": [
                        min(l.price for l in c.listings),
                        max(l.price for l in c.listings),
                    ],
                    "best_offer": {
                        "source": best.listing.source,
                        "title": best.listing.title,
                        "price": best.listing.price,
                        "delivery_days": best.listing.delivery_days,
                        "seller_rating": best.listing.seller_rating,
                        "topsis_score": best.score,
                        "why": best.contributions,
                    },
                    "all_offers": [
                        {
                            "rank": r.rank,
                            "source": r.listing.source,
                            "source_item_id": r.listing.source_item_id,
                            "title": r.listing.title,
                            "price": r.listing.price,
                            "delivery_days": r.listing.delivery_days,
                            "seller_rating": r.listing.seller_rating,
                            "url": r.listing.url,
                            "score": r.score,
                            "original_price": (r.listing.raw or {}).get("_original_price"),
                            "original_currency": (r.listing.raw or {}).get("_original_currency"),
                        }
                        for r in ranked
                    ],
                }
            )
        results.sort(key=lambda r: (-r["num_offers"], r["price_range"][0]))
        return results
