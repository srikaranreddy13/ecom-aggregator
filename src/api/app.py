"""
HTTP API for the aggregator.

Before this, the only way to see results was `scripts/build_dashboard.py`,
which baked a snapshot into a static HTML file. That is fine for a screenshot
and useless for anything else: a reviewer cannot type their own query, the
sliders re-implement TOPSIS in JavaScript, and nothing can be integrated with.

    uvicorn src.api.app:app --reload
    python -m scripts.serve

Endpoints:
    GET  /health                          liveness + data-asset diagnostics
    GET  /config                          effective configuration (secrets redacted)
    POST /search                          rank offers under a weight vector
    GET  /search                          same, query-string form, for the browser
    GET  /products                        clusters without ranking
    GET  /price-history/{source}/{id}     one item's series
    GET  /tracked-items                   everything with enough history to model
    GET  /docs                            generated OpenAPI UI

Design notes:
  * ranking runs server-side, so the API and the paper cannot disagree about
    what TOPSIS returns
  * every response carries `diagnostics`, so a degraded source is visible in
    the response rather than only in the logs
  * /health reports `flat_series` and FX-snapshot age -- the two silent failure
    modes this project actually has
"""

from __future__ import annotations

import time

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ..adapters.errors import AdapterError
from ..config import ROOT, settings
from ..logging_setup import configure_logging, get_logger
from ..matching.resolver import (
    extract_colour,
    extract_generation,
    extract_model_number,
    extract_storage_gb,
)
from ..money import rate_table, rate_table_age_days
from ..ranking.mcdm import DEFAULT_WEIGHTS
from ..storage import db
from .deps import VERSION, resources
from .schemas import (
    HealthResponse,
    PriceHistoryResponse,
    SearchRequest,
    SearchResponse,
    TrackedItemOut,
    Weights,
)

log = get_logger(__name__)

# A snapshot older than this is flagged on /health. Not an error -- FX drift of
# a few percent does not change a ranking -- but it should never be invisible.
FX_STALE_DAYS = 90


def create_app() -> FastAPI:
    configure_logging()

    app = FastAPI(
        title="E-Commerce Aggregator API",
        version=VERSION,
        description=(
            "Cross-marketplace product aggregation with semantic entity "
            "resolution and multi-criteria (TOPSIS) ranking."
        ),
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list(),
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    # ---------------------------------------------------------- lifecycle

    @app.on_event("startup")
    def _startup() -> None:
        resources.startup()

    @app.on_event("shutdown")
    def _shutdown() -> None:
        resources.shutdown()

    # ---------------------------------------------------------- middleware

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        started = time.monotonic()
        response = await call_next(request)
        took = (time.monotonic() - started) * 1000
        log.info("request",
                 extra={"method": request.method, "path": request.url.path,
                        "status": response.status_code, "took_ms": round(took, 1)})
        response.headers["X-Response-Time-ms"] = f"{took:.1f}"
        return response

    # ---------------------------------------------------------- errors

    @app.exception_handler(AdapterError)
    async def adapter_error_handler(request: Request, exc: AdapterError):
        # 502: we are fine, an upstream source is not.
        return JSONResponse(
            status_code=502,
            content={"error": "upstream_source_failed", "detail": str(exc)},
        )

    # ---------------------------------------------------------- health

    @app.get("/health", response_model=HealthResponse, tags=["ops"])
    def health():
        """Liveness plus the diagnostics that actually predict trouble here."""
        conn = resources.connection()
        series = db.series_stats(conn)
        warnings: list[str] = []

        if series["observations"] == 0:
            warnings.append(
                "price_history is empty -- run scripts/backfill_prices.py "
                "or scripts/collect_prices.py"
            )
        if series["flat_series"]:
            warnings.append(
                f"{series['flat_series']} price series have zero variance and "
                f"cannot be forecast"
            )
        if series["observations"] and series["real_observations"] == 0:
            warnings.append(
                "all price observations are synthetic -- any forecast accuracy "
                "figure measures the simulator, not the market"
            )
        age = rate_table_age_days()
        if age > FX_STALE_DAYS:
            warnings.append(
                f"FX snapshot is {age} days old -- run scripts/update_fx_rates.py"
            )
        if any(getattr(a, "is_synthetic", False) for a in resources.adapters):
            warnings.append("serving results from the synthetic market")

        return HealthResponse(
            status="degraded" if warnings else "ok",
            version=VERSION,
            embedder_backend=resources.embedder.backend if resources.embedder else "unset",
            adapters=[
                {"name": a.name,
                 "synthetic": getattr(a, "is_synthetic", False),
                 "cacheable": getattr(a, "cacheable", False),
                 "healthy": _safe_health(a)}
                for a in resources.adapters
            ],
            price_history=series,
            cache=db.cache_stats(conn),
            fx={"as_of": rate_table().as_of.isoformat(),
                "age_days": age,
                "base_currency": settings.base_currency},
            warnings=warnings,
        )

    @app.get("/config", tags=["ops"])
    def config():
        """Effective configuration with secrets redacted. Answers 'why is it
        behaving differently on your machine than mine' in one request."""
        return settings.describe()

    # ---------------------------------------------------------- search

    def _run_search(query: str, weights: Weights, limit_per_source: int,
                    normalisation: str) -> SearchResponse:
        if weights.is_degenerate():
            raise HTTPException(
                status_code=422,
                detail="At least one weight must be greater than zero.",
            )
        started = time.monotonic()
        pipe = resources.pipeline()
        results = pipe.search(
            query,
            weights=weights.as_dict(),
            limit_per_source=limit_per_source,
            normalisation=normalisation,
        )
        return SearchResponse(
            query=query,
            normalisation=normalisation,
            weights=weights.as_dict(),
            count=len(results),
            took_ms=round((time.monotonic() - started) * 1000, 1),
            diagnostics=pipe.stats,
            results=results,
        )

    @app.post("/search", response_model=SearchResponse, tags=["search"])
    def search_post(req: SearchRequest):
        """Rank offers for a query under a user weight vector."""
        return _run_search(req.query, req.weights, req.limit_per_source,
                           req.normalisation)

    @app.get("/search", response_model=SearchResponse, tags=["search"])
    def search_get(
        q: str = Query("", max_length=200, description="product query"),
        price: float = Query(DEFAULT_WEIGHTS["price"], ge=0, le=1),
        delivery_days: float = Query(DEFAULT_WEIGHTS["delivery_days"], ge=0, le=1),
        seller_rating: float = Query(DEFAULT_WEIGHTS["seller_rating"], ge=0, le=1),
        limit_per_source: int = Query(25, ge=1, le=200),
        normalisation: str = Query("minmax", pattern="^(minmax|vector)$"),
    ):
        """Query-string form, so the dashboard and a browser can call it directly.

        Set normalisation=vector to reproduce the textbook-TOPSIS failure where
        every weight vector collapses to the same winner.
        """
        weights = Weights(price=price, delivery_days=delivery_days,
                          seller_rating=seller_rating)
        return _run_search(q, weights, limit_per_source, normalisation)

    @app.get("/products", tags=["search"])
    def products(
        q: str = Query("", max_length=200),
        limit_per_source: int = Query(25, ge=1, le=200),
    ):
        """Resolved clusters without ranking -- shows entity resolution alone.

        Each offer carries the ATTRIBUTES the resolver extracted from its title
        and the BLOCK KEY those attributes produced. That is what makes Stage 1
        inspectable: two listings with different block keys can never merge, no
        matter how similar their text, and here you can see exactly which
        attribute separated them.
        """
        pipe = resources.pipeline()
        listings = pipe.fetch(q, limit_per_source=limit_per_source)
        clusters = pipe.resolve(listings)

        def describe(l):
            d = l.to_dict()
            d["attributes"] = {
                "brand": (l.brand or "").lower() or None,
                "storage_gb": extract_storage_gb(l.title),
                "colour": extract_colour(l.title),
                "model_number": extract_model_number(l.title),
                "generation": extract_generation(l.title),
            }
            # Only brand, storage and colour form the block key. Model number and
            # generation act as must-not-link constraints *inside* a block --
            # a different mechanism, so they are reported separately.
            d["block_key"] = "|".join(
                str(d["attributes"][k] if d["attributes"][k] is not None else "-")
                for k in ("brand", "storage_gb", "colour")
            )
            return d

        return {
            "query": q,
            "listings": len(listings),
            "clusters": len(clusters),
            "diagnostics": pipe.stats,
            "resolver": {
                "backend": pipe.resolver.embedder.backend,
                "similarity_threshold": pipe.resolver.similarity_threshold,
                "blocking_enabled": pipe.resolver.use_blocking,
                "model_constraint_enabled": pipe.resolver.enforce_model_constraint,
                "block_key_fields": ["brand", "storage_gb", "colour"],
                "must_not_link_fields": ["model_number", "generation"],
            },
            "products": [
                {
                    "cluster_id": c.cluster_id,
                    "canonical_title": c.canonical_title,
                    "num_offers": len(c),
                    "min_price": c.min_price,
                    "max_price": max(l.price for l in c.listings),
                    "sources": sorted({l.source for l in c.listings}),
                    "offers": [describe(l) for l in
                               sorted(c.listings, key=lambda x: x.price)],
                }
                for c in clusters
            ],
        }

    # ---------------------------------------------------------- price history

    @app.get("/price-history/{source}/{source_item_id}",
             response_model=PriceHistoryResponse, tags=["prices"])
    def price_history(source: str, source_item_id: str):
        """Observed price series for one item."""
        conn = resources.connection()
        series = db.price_series(conn, source, source_item_id)
        if not series:
            raise HTTPException(
                status_code=404,
                detail=f"No price history for {source}/{source_item_id}. "
                       f"See /tracked-items for what is available.",
            )
        row = conn.execute(
            "SELECT MAX(title) title, MAX(currency) currency, "
            "       MAX(is_synthetic) synth, MIN(price) lo, MAX(price) hi, "
            "       AVG(price) avg FROM price_history "
            "WHERE source = ? AND source_item_id = ?",
            (source, source_item_id),
        ).fetchone()

        return PriceHistoryResponse(
            source=source,
            source_item_id=source_item_id,
            title=row["title"],
            currency=row["currency"],
            is_synthetic=bool(row["synth"]),
            observations=len(series),
            min_price=row["lo"],
            max_price=row["hi"],
            avg_price=round(row["avg"], 2) if row["avg"] is not None else None,
            series=[{"observed_on": d, "price": p} for d, p in series],
        )

    @app.get("/tracked-items", response_model=list[TrackedItemOut], tags=["prices"])
    def tracked_items(min_observations: int = Query(2, ge=1, le=10_000)):
        """Items with enough history to model, longest series first."""
        rows = db.tracked_items(resources.connection(),
                               min_observations=min_observations)
        return [
            TrackedItemOut(
                source=r["source"],
                source_item_id=r["source_item_id"],
                title=r["title"],
                currency=r["currency"],
                observations=r["n"],
                first_seen=r["first_seen"],
                last_seen=r["last_seen"],
                min_price=round(r["min_price"], 2),
                max_price=round(r["max_price"], 2),
                avg_price=round(r["avg_price"], 2),
                is_synthetic=bool(r["is_synthetic"]),
            )
            for r in rows
        ]

    # ---------------------------------------------------------- frontend
    # Mounted LAST, at "/", so it cannot shadow an API route: Starlette matches
    # routes in registration order, and a mount at "/" would otherwise swallow
    # every path registered after it.
    #
    # html=True makes the mount serve index.html for "/" and return 404 (not a
    # directory listing) for anything missing.
    ui_dir = ROOT / "web" / "app"
    if ui_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(ui_dir), html=True), name="ui")
        log.info("frontend mounted", extra={"path": str(ui_dir)})
    else:
        log.warning("frontend directory not found, API-only mode",
                    extra={"expected": str(ui_dir)})

    return app


def _safe_health(adapter) -> bool:
    """An adapter's health check must never take the health endpoint down."""
    try:
        return bool(adapter.health())
    except Exception as exc:  # noqa: BLE001
        log.warning("adapter health check raised",
                    extra={"adapter": adapter.name, "error": str(exc)})
        return False


app = create_app()
