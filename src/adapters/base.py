"""
Source adapter contract.

Every marketplace -- live API, scraper, or cached file -- implements this same
interface. Nothing downstream (matching, ranking, recommendation) knows or cares
where a listing came from. This is what lets us swap a mock source for a live
eBay feed without touching the ML code.
"""

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field, fields as dataclass_fields
from datetime import datetime, timezone
from typing import Any


@dataclass
class Listing:
    """Canonical product listing. Every adapter must normalise to this shape."""

    source: str                 # "ebay", "mock", "flipkart", ...
    source_item_id: str         # unique within that source
    title: str
    price: float                # in a single currency after normalisation
    currency: str = "INR"
    delivery_days: int | None = None
    seller_rating: float | None = None      # 0.0 - 5.0
    seller_name: str | None = None
    url: str | None = None
    image_url: str | None = None
    brand: str | None = None
    fetched_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_item_id}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("raw", None)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Listing":
        """Rebuild a Listing from `to_dict()` output.

        Needed by the response cache, which round-trips listings through JSON.
        Unknown keys are ignored so a cache written by an older version of the
        schema still loads instead of raising TypeError.
        """
        known = {f.name for f in dataclass_fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


class SourceAdapter(ABC):
    """Base class for every data source."""

    name: str = "unnamed"

    # True for simulated sources. Any evaluation that mixes sources MUST report
    # how many rows came from a synthetic one -- see synthetic_adapter.py.
    is_synthetic: bool = False

    # Whether responses may be served from the SQLite response cache. Live APIs
    # want this; a local file adapter gains nothing from it.
    cacheable: bool = False

    @abstractmethod
    def search(self, query: str, limit: int = 25) -> list[Listing]:
        """Return normalised listings for a search query."""
        raise NotImplementedError

    def health(self) -> bool:
        """Cheap check that the source is reachable. Override for live sources."""
        return True

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r}>"
