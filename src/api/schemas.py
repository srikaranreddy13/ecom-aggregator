"""
Request/response models for the HTTP API.

These are the contract. Two reasons they are declared rather than returning
bare dicts:

  * validation at the edge -- a weight of -3 or a limit of 10_000_000 is
    rejected with a 422 before it reaches the pipeline, instead of producing a
    confusing failure deep inside TOPSIS
  * the OpenAPI schema at /docs is generated from them, so the API documents
    itself and the dashboard's fetch calls can be checked against it
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class Weights(BaseModel):
    """User's MCDM preference vector. Normalised downstream, so any positive
    scale works -- {8,1,1} and {0.8,0.1,0.1} rank identically."""

    price: float = Field(0.5, ge=0, le=1, description="importance of a low price")
    delivery_days: float = Field(0.25, ge=0, le=1, description="importance of fast delivery")
    seller_rating: float = Field(0.25, ge=0, le=1, description="importance of a trusted seller")

    @field_validator("price", "delivery_days", "seller_rating")
    @classmethod
    def _finite(cls, v: float) -> float:
        if v != v:  # NaN
            raise ValueError("weight must be a number")
        return v

    def as_dict(self) -> dict[str, float]:
        return {
            "price": self.price,
            "delivery_days": self.delivery_days,
            "seller_rating": self.seller_rating,
        }

    def is_degenerate(self) -> bool:
        return sum(self.as_dict().values()) <= 0


class SearchRequest(BaseModel):
    query: str = Field("", max_length=200,
                       description="free-text product query; empty returns the whole catalogue")
    weights: Weights = Field(default_factory=Weights)
    limit_per_source: int = Field(25, ge=1, le=200)
    normalisation: str = Field("minmax", pattern="^(minmax|vector)$",
                               description="'minmax' (responsive) or 'vector' (textbook TOPSIS)")


class OfferOut(BaseModel):
    rank: int
    source: str
    source_item_id: str | None = None
    title: str | None = None
    price: float
    delivery_days: int | None = None
    seller_rating: float | None = None
    url: str | None = None
    score: float
    original_price: float | None = None
    original_currency: str | None = None


class BestOfferOut(BaseModel):
    source: str
    title: str
    price: float
    delivery_days: int | None = None
    seller_rating: float | None = None
    topsis_score: float
    why: dict[str, float]


class ProductOut(BaseModel):
    cluster_id: str
    product: str
    num_offers: int
    sources: list[str]
    currency: str
    price_range: list[float]
    best_offer: BestOfferOut
    all_offers: list[OfferOut]


class SearchResponse(BaseModel):
    query: str
    normalisation: str
    weights: dict[str, float]
    count: int
    took_ms: float
    diagnostics: dict = Field(
        default_factory=dict,
        description="per-source counts, cache hits and adapter failures for this query",
    )
    results: list[ProductOut]


class PricePoint(BaseModel):
    observed_on: str
    price: float


class PriceHistoryResponse(BaseModel):
    source: str
    source_item_id: str
    title: str | None = None
    currency: str | None = None
    is_synthetic: bool
    observations: int
    min_price: float | None = None
    max_price: float | None = None
    avg_price: float | None = None
    series: list[PricePoint]


class TrackedItemOut(BaseModel):
    source: str
    source_item_id: str
    title: str | None = None
    currency: str | None = None
    observations: int
    first_seen: str
    last_seen: str
    min_price: float
    max_price: float
    avg_price: float
    is_synthetic: bool


class HealthResponse(BaseModel):
    status: str
    version: str
    embedder_backend: str
    adapters: list[dict]
    price_history: dict
    cache: dict
    fx: dict
    warnings: list[str]


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
