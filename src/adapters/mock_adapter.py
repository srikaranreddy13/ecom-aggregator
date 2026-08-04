"""Offline adapter backed by a local JSON file. Used for tests and demos."""

import json
from pathlib import Path

from ..config import settings
from .base import Listing, SourceAdapter

DEFAULT_PATH = settings.sample_data_path


class MockAdapter(SourceAdapter):
    name = "mock"

    # Reading a local file is already cheap; caching it would only add a
    # staleness problem for no gain.
    cacheable = False

    def __init__(self, path: Path | str | None = None, only_source: str | None = None):
        self.path = Path(path) if path is not None else settings.sample_data_path
        self.only_source = only_source
        self._rows = json.loads(self.path.read_text(encoding="utf-8"))

    def search(self, query: str, limit: int = 25) -> list[Listing]:
        q = query.lower().strip()
        out: list[Listing] = []
        for r in self._rows:
            if self.only_source and r["source"] != self.only_source:
                continue
            # naive keyword prefilter -- mirrors what a real marketplace search does
            if q and not any(tok in r["title"].lower() for tok in q.split()):
                continue
            out.append(
                Listing(
                    source=r["source"],
                    source_item_id=r["source_item_id"],
                    title=r["title"],
                    price=float(r["price"]),
                    delivery_days=r.get("delivery_days"),
                    seller_rating=r.get("seller_rating"),
                    seller_name=r.get("seller_name"),
                    brand=r.get("brand"),
                    raw=r,
                )
            )
            if len(out) >= limit:
                break
        return out
