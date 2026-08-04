"""
Pipeline tests: caching, currency, and the failure policy.

The failure-policy tests are the important ones. "One dead source degrades the
result but never fails the query" is a claim, and an untested claim about error
handling is usually false.
"""

import pytest

from src.adapters.base import Listing, SourceAdapter
from src.adapters.errors import AdapterAuthError, AdapterUnavailable
from src.matching.resolver import EntityResolver
from src.pipeline import AggregatorPipeline


class StubAdapter(SourceAdapter):
    """Returns canned listings and counts how often it was actually called."""

    cacheable = True

    def __init__(self, name, listings=None, raises=None):
        self.name = name
        self._listings = listings or []
        self._raises = raises
        self.calls = 0

    def search(self, query, limit=25):
        self.calls += 1
        if self._raises:
            raise self._raises
        return list(self._listings)


def _l(source, item, price, currency="INR", **kw):
    kw.setdefault("delivery_days", 3)
    kw.setdefault("seller_rating", 4.5)
    return Listing(source, item, kw.pop("title", "Apple iPhone 15 (128GB, Blue)"),
                   price, currency=currency, **kw)


@pytest.fixture
def pipe_factory(temp_db, embedder):
    def make(adapters, **kw):
        return AggregatorPipeline(
            adapters=adapters,
            resolver=EntityResolver(embedder),
            conn=temp_db,
            **kw,
        )
    return make


# ---------------------------------------------------------------- caching


def test_second_identical_query_hits_the_cache(pipe_factory):
    """The cache existed but nothing called it -- every query re-hit the source."""
    ad = StubAdapter("live", [_l("live", "1", 60000.0)])
    pipe = pipe_factory([ad])

    pipe.fetch("iphone")
    assert ad.calls == 1
    assert pipe.stats["cache_hits"] == []

    pipe.fetch("iphone")
    assert ad.calls == 1, "adapter was called again despite a warm cache"
    assert pipe.stats["cache_hits"] == ["live"]


def test_different_query_is_a_separate_cache_entry(pipe_factory):
    ad = StubAdapter("live", [_l("live", "1", 60000.0)])
    pipe = pipe_factory([ad])
    pipe.fetch("iphone")
    pipe.fetch("samsung")
    assert ad.calls == 2


def test_cache_key_ignores_case_and_padding(pipe_factory):
    ad = StubAdapter("live", [_l("live", "1", 60000.0)])
    pipe = pipe_factory([ad])
    pipe.fetch("iPhone")
    pipe.fetch("  iphone  ")
    assert ad.calls == 1


def test_expired_cache_refetches(pipe_factory):
    ad = StubAdapter("live", [_l("live", "1", 60000.0)])
    pipe = pipe_factory([ad], cache_ttl=0)
    pipe.fetch("iphone")
    pipe.fetch("iphone")
    assert ad.calls == 2


def test_non_cacheable_adapter_is_never_cached(pipe_factory):
    ad = StubAdapter("localfile", [_l("localfile", "1", 60000.0)])
    ad.cacheable = False
    pipe = pipe_factory([ad])
    pipe.fetch("iphone")
    pipe.fetch("iphone")
    assert ad.calls == 2


def test_cached_listings_are_equivalent(pipe_factory):
    ad = StubAdapter("live", [_l("live", "1", 60000.0, delivery_days=2,
                                 seller_rating=4.9)])
    pipe = pipe_factory([ad])
    fresh = pipe.fetch("iphone")
    cached = pipe.fetch("iphone")

    assert [l.key for l in fresh] == [l.key for l in cached]
    assert [l.price for l in fresh] == [l.price for l in cached]
    assert [l.delivery_days for l in fresh] == [l.delivery_days for l in cached]
    assert [l.seller_rating for l in fresh] == [l.seller_rating for l in cached]


# ---------------------------------------------------------------- currency


def test_fetch_converts_to_base_currency(pipe_factory):
    ad = StubAdapter("ebay", [_l("ebay", "1", 699.0, currency="USD")])
    listings = pipe_factory([ad], base_currency="INR").fetch("iphone")

    assert listings[0].currency == "INR"
    assert listings[0].price > 10_000
    assert listings[0].raw["_original_currency"] == "USD"


def test_mixed_currency_ranking_is_correct(pipe_factory):
    """End-to-end version of the cross-currency bug."""
    inr = StubAdapter("local", [_l("local", "1", 60_000.0, currency="INR")])
    usd = StubAdapter("ebay", [_l("ebay", "2", 699.0, currency="USD")])

    results = pipe_factory([inr, usd]).search(
        "iphone", weights={"price": 0.95, "delivery_days": 0.025,
                           "seller_rating": 0.025})

    # Both listings share a title so they resolve into one cluster.
    assert results[0]["num_offers"] == 2
    # 699 USD ~ 61,163 INR > 60,000, so the local listing must win on price.
    assert results[0]["best_offer"]["source"] == "local"
    assert results[0]["currency"] == "INR"


def test_history_is_stored_in_base_currency(pipe_factory, temp_db):
    ad = StubAdapter("ebay", [_l("ebay", "1", 699.0, currency="USD")])
    pipe_factory([ad], base_currency="INR").fetch("iphone")

    row = temp_db.execute("SELECT price, currency FROM price_history").fetchone()
    assert row["currency"] == "INR"
    assert row["price"] > 10_000


# ---------------------------------------------------------------- failures


def test_one_dead_source_does_not_fail_the_query(pipe_factory):
    good = StubAdapter("good", [_l("good", "1", 60000.0)])
    dead = StubAdapter("dead", raises=AdapterUnavailable("503", source="dead"))

    listings = pipe_factory([good, dead]).fetch("iphone")
    assert len(listings) == 1
    assert listings[0].source == "good"


def test_failures_are_reported_in_diagnostics(pipe_factory):
    """A degraded result must be visible to the caller, not only in the logs."""
    good = StubAdapter("good", [_l("good", "1", 60000.0)])
    dead = StubAdapter("dead", raises=AdapterUnavailable("503", source="dead"))

    pipe = pipe_factory([good, dead])
    pipe.fetch("iphone")
    assert "dead" in pipe.stats["failures"]
    assert pipe.stats["per_source"] == {"good": 1}


def test_auth_failure_disables_the_adapter(pipe_factory):
    """Retrying a bad credential on every query is pure wasted latency."""
    bad = StubAdapter("bad", raises=AdapterAuthError("no key", source="bad"))
    good = StubAdapter("good", [_l("good", "1", 60000.0)])
    pipe = pipe_factory([good, bad])

    pipe.fetch("q1")
    assert bad.calls == 1
    pipe.fetch("q2")
    assert bad.calls == 1, "disabled adapter was queried again"
    assert "bad" in pipe.stats["disabled_adapters"]


def test_untyped_adapter_exception_is_contained(pipe_factory):
    """An adapter that raises something unexpected must not kill the query."""
    rogue = StubAdapter("rogue", raises=RuntimeError("something odd"))
    good = StubAdapter("good", [_l("good", "1", 60000.0)])

    listings = pipe_factory([good, rogue]).fetch("iphone")
    assert len(listings) == 1


def test_all_sources_dead_returns_empty_not_exception(pipe_factory):
    dead1 = StubAdapter("d1", raises=AdapterUnavailable("x", source="d1"))
    dead2 = StubAdapter("d2", raises=AdapterUnavailable("x", source="d2"))

    pipe = pipe_factory([dead1, dead2])
    assert pipe.fetch("iphone") == []
    assert pipe.search("iphone") == []


# ---------------------------------------------------------------- diagnostics


def test_stats_flag_synthetic_sources(pipe_factory):
    from src.adapters.synthetic_adapter import SyntheticMarketAdapter

    pipe = pipe_factory([SyntheticMarketAdapter("sim_standard", seed=1)])
    pipe.fetch("iphone")
    assert pipe.stats["synthetic_sources"] == ["sim_standard"]


def test_record_history_can_be_disabled(pipe_factory, temp_db):
    """API traffic must not write price history -- it would sample the series by
    user activity instead of by time."""
    ad = StubAdapter("live", [_l("live", "1", 60000.0)])
    pipe_factory([ad], record_history=False).fetch("iphone")
    assert temp_db.execute("SELECT COUNT(*) c FROM price_history").fetchone()["c"] == 0


def test_search_exposes_conversion_provenance(pipe_factory):
    ad = StubAdapter("ebay", [_l("ebay", "1", 699.0, currency="USD")])
    results = pipe_factory([ad]).search("iphone")
    offer = results[0]["all_offers"][0]
    assert offer["original_currency"] == "USD"
    assert offer["original_price"] == 699.0
