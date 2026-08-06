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
from ..config import settings
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
        self._local = threading.local()

    # ---------------- lifecycle ----------------

    def startup(self) -> None:
        log.info("api starting", extra={"version": VERSION,
                                       "config": settings.describe()})
        self.embedder = Embedder()
        self.resolver = EntityResolver(self.embedder)
        self.adapters = build_adapters()

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


def build_adapters() -> list[SourceAdapter]:
    """Choose the offer sources, most-real-first.

    Order matters and is a judgement about honesty, not convenience:

      1. eBay -- real sellers, real prices, real product URLs. Needs a key.
      2. Open Prices -- REAL shop prices for real groceries, no key at all.
         Chosen over the simulation whenever it is reachable, because a real
         price beats an invented one even when the category range is narrower.
      3. Synthetic market -- last resort, and it announces itself.

    Override with SOURCE=ebay|openprices|synthetic to force one.
    """
    forced = settings.source.lower()
    adapters: list[SourceAdapter] = []

    if forced in ("auto", "ebay") and settings.has_ebay_credentials():
        from ..adapters.ebay_adapter import EbayAdapter

        ebay = EbayAdapter()
        if ebay.health():
            adapters.append(ebay)
            log.info("using live eBay sellers")
        else:
            log.warning("ebay credentials set but health check failed; skipping")

    if not adapters and forced in ("auto", "openprices"):
        from ..openprices import OpenPricesAdapter

        op = OpenPricesAdapter()
        if op.health():
            adapters.append(op)
            log.info("using Open Prices -- real shop prices, no API key")
        elif forced == "openprices":
            log.error("Open Prices unreachable and SOURCE=openprices was forced")

    if not adapters:
        adapters = list(synthetic_market())
        log.warning(
            "serving from the SYNTHETIC market -- prices are simulated",
            extra={"sellers": [a.name for a in adapters]},
        )

    return adapters


resources = Resources()
