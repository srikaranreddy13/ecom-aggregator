"""
Shared, process-lifetime resources for the API.

WHY THIS FILE EXISTS. Two things in the pipeline are expensive to construct and
cheap to reuse:

  * the Embedder -- loading sentence-transformers takes tens of seconds. Doing
    it per request would make the API unusable.
  * the SQLite connection -- opening one runs the schema script and migrations.

So both are created once at startup and held. The resolver depends on the
embedder, so it is shared too; it is stateless across calls, which makes that
safe.

SQLite and threads: a connection created on one thread cannot be used on
another by default, and FastAPI runs sync endpoints in a threadpool. Rather
than pass check_same_thread=False and hope, we hand out one connection per
thread from thread-local storage. Reads dominate here, so this is simpler and
safer than a pooling library.
"""

from __future__ import annotations

import threading

from ..adapters.base import SourceAdapter
from ..adapters.synthetic_adapter import synthetic_market
from ..catalogue import get_catalogue
from ..config import settings
from ..recommender import Recommender
from ..logging_setup import get_logger
from ..matching.embedder import Embedder
from ..matching.resolver import EntityResolver
from ..pipeline import AggregatorPipeline
from ..storage import db

log = get_logger(__name__)

VERSION = "0.2.0"


class Resources:
    """Container for everything built once per process."""

    def __init__(self) -> None:
        self.embedder: Embedder | None = None
        self.resolver: EntityResolver | None = None
        self.adapters: list[SourceAdapter] = []
        self.catalogue = None
        self.recommender: Recommender | None = None
        self._local = threading.local()

    # ---------------- lifecycle ----------------

    def startup(self) -> None:
        log.info("api starting", extra={"version": VERSION,
                                       "config": settings.describe()})
        self.embedder = Embedder()
        self.resolver = EntityResolver(self.embedder)
        # Offers and catalogue are chosen together and held together. Every
        # consumer -- /search, /categories, /recommendations -- must read the
        # SAME catalogue the offers came from, or the UI silently disagrees with
        # itself. See build_source().
        self.adapters, self.catalogue = build_source()
        self.recommender = Recommender(self.catalogue.all_products())

        # Touch the DB once on the main thread so schema creation and migration
        # happen at startup, where a failure is loud, not inside a request.
        self.connection()

        log.info("api ready",
                 extra={"backend": self.embedder.backend,
                        "threshold": self.resolver.similarity_threshold,
                        "adapters": [a.name for a in self.adapters]})

    def shutdown(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
        log.info("api shutdown")

    # ---------------- per-thread DB ----------------

    def connection(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = db.connect()
            self._local.conn = conn
        return conn

    # ---------------- pipeline ----------------

    def pipeline(self, record_history: bool = False) -> AggregatorPipeline:
        """A pipeline bound to this thread's connection.

        `record_history` defaults to False for API traffic. A daily snapshot is
        a scheduled job (scripts/collect_prices.py); letting every browser
        request write into price_history would give the forecasting module a
        series sampled by user activity rather than by time -- popular products
        would appear to have more history than quiet ones, purely as an artefact
        of traffic.
        """
        return AggregatorPipeline(
            adapters=self.adapters,
            resolver=self.resolver,
            conn=self.connection(),
            record_history=record_history,
        )


def build_source() -> tuple[list[SourceAdapter], object]:
    """Choose a COHERENT (offers, catalogue) pair.

    THIS RETURNS BOTH TOGETHER FOR A REASON, and it is a bug fix.

    A previous version picked the offer source "most-real-first" and let the
    catalogue be chosen independently. The result was incoherent: offers came from
    Open Prices (French grocery shops) while the category chips and the
    recommender came from the DummyJSON catalogue. So every category button
    returned the same rice and cheese, and recommendations were always empty
    because the recommender matched titles against a catalogue that had nothing
    to do with the products on screen.

    Nothing errored. Both halves were individually correct. The app was broken
    because they disagreed -- which is why they are now selected as one unit and
    cannot drift apart.

    Modes (SOURCE=...):

      shop        DummyJSON catalogue + simulated competing sellers. THE DEFAULT.
                  194 products over 24 categories, real images, working category
                  browse, working recommendations, and three genuinely different
                  profile winners per product. Prices are simulated.
      openprices  Open Prices catalogue + real shop offers. REAL prices from real
                  named shops, no API key. Groceries only, and price is the only
                  criterion that varies.
      ebay        Live eBay sellers (needs credentials).
    """
    mode = settings.source.lower()
    if mode == "auto":
        if settings.has_serpapi():
            mode = "serpapi"
        elif settings.has_ebay_credentials():
            mode = "ebay"
        else:
            mode = "shop"

    if mode == "serpapi":
        from ..adapters.serpapi_adapter import SerpApiShoppingAdapter

        adapter = SerpApiShoppingAdapter()
        if adapter.health():
            left = adapter._searches_left
            log.info("source: Google Shopping via SerpApi -- real retailers, "
                     "real prices, any product",
                     extra={"country": adapter.country, "searches_left": left})
            if left is not None and left < 25:
                log.warning("SerpApi quota nearly exhausted -- new queries will "
                            "start failing; cached ones keep working",
                            extra={"searches_left": left})
            # SerpApi supplies OFFERS for whatever the user types; it has no
            # browsable catalogue. The local catalogue is kept alongside it so
            # category browse and recommendations still work -- and because they
            # are returned together, they cannot silently disagree.
            return [adapter], get_catalogue()
        log.error("SerpApi key missing or rejected, falling back to shop mode")
        mode = "shop"

    if mode == "ebay":
        from ..adapters.ebay_adapter import EbayAdapter

        ebay = EbayAdapter()
        if ebay.health():
            log.info("source: live eBay sellers")
            return [ebay], get_catalogue()
        log.warning("ebay unavailable, falling back to the shop mode")
        mode = "shop"

    if mode == "openprices":
        from ..openprices import OpenPricesAdapter, OpenPricesCatalogue

        catalogue = OpenPricesCatalogue()
        adapter = OpenPricesAdapter(catalogue=catalogue)
        if adapter.health():
            log.info("source: Open Prices -- real shop prices, no API key. "
                     "Groceries only; price is the only varying criterion.")
            return [adapter], catalogue
        log.error("Open Prices unreachable, falling back to the shop mode")

    catalogue = get_catalogue()
    adapters = list(synthetic_market(products=catalogue.all_products()))
    log.info("source: shop mode -- real products and images, simulated sellers",
             extra={"sellers": [a.name for a in adapters],
                    "catalogue": catalogue.name,
                    "products": len(catalogue.all_products())})
    return adapters, catalogue


def build_adapters() -> list[SourceAdapter]:
    """Backwards-compatible shim: offers only."""
    return build_source()[0]


resources = Resources()
