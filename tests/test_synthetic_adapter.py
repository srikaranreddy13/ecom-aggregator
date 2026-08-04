"""
Synthetic market tests.

The two properties that matter are DETERMINISM (without it a backfilled history
is not reproducible and no figure can be regenerated) and VARIANCE (the whole
reason this adapter exists -- MockAdapter produced a flat series).
"""

from datetime import date, timedelta

import pytest

from src.adapters.synthetic_adapter import (
    SELLER_PROFILES,
    SyntheticMarketAdapter,
    date_range,
    synthetic_market,
)

DAY = date(2026, 6, 15)


@pytest.fixture(scope="module")
def adapter():
    return SyntheticMarketAdapter("sim_standard", seed=42)


# ---------------------------------------------------------------- determinism


def test_same_inputs_give_same_price(adapter):
    a = adapter.price_on("iphone-15-128", 66999.0, DAY)
    b = adapter.price_on("iphone-15-128", 66999.0, DAY)
    assert a == b


def test_determinism_survives_a_new_instance():
    """Must not depend on process state -- this is why blake2b replaced hash().

    Python salts str hashing per process, so an implementation built on hash()
    would pass within one run and silently return different prices tomorrow.
    """
    a = SyntheticMarketAdapter("sim_standard", seed=42).price_on("x", 1000.0, DAY)
    b = SyntheticMarketAdapter("sim_standard", seed=42).price_on("x", 1000.0, DAY)
    assert a == b


def test_different_seed_gives_different_history():
    a = SyntheticMarketAdapter("sim_standard", seed=1).price_on("x", 1000.0, DAY)
    b = SyntheticMarketAdapter("sim_standard", seed=2).price_on("x", 1000.0, DAY)
    assert a != b


def test_backfill_matches_what_live_collection_would_have_seen():
    """A price generated for a past date must equal the price an adapter
    configured with as_of=that date returns from search(). If these diverge, a
    backfilled series is not the series you would have collected."""
    past = date(2026, 5, 1)
    a = SyntheticMarketAdapter("sim_standard", seed=7, as_of=past)
    from_search = {l.source_item_id: l.price for l in a.search("", limit=100)}
    from_time_machine = {l.source_item_id: l.price
                         for l in a.listings_on(past, "", limit=100)}
    assert from_search == from_time_machine


# ---------------------------------------------------------------- variance


def test_prices_actually_move(adapter):
    """THE POINT OF THIS ADAPTER. MockAdapter returned a constant here."""
    prices = [adapter.price_on("iphone-15-128", 66999.0, DAY + timedelta(days=i))
              for i in range(90)]
    assert len(set(prices)) > 60, "series is too flat to forecast"

    spread = (max(prices) - min(prices)) / (sum(prices) / len(prices))
    assert 0.01 < spread < 0.60, f"spread {spread:.3f} is not plausible"


def test_series_is_autocorrelated_not_white_noise(adapter):
    """Consecutive days must be closer than distant days.

    Independent per-day draws would produce a sawtooth no real price shows, and
    would make any forecasting result meaningless (nothing to learn).
    """
    prices = [adapter.price_on("x", 50000.0, DAY + timedelta(days=i))
              for i in range(120)]
    adjacent = sum(abs(prices[i + 1] - prices[i]) for i in range(len(prices) - 1))
    adjacent /= len(prices) - 1
    distant = sum(abs(prices[i + 30] - prices[i]) for i in range(len(prices) - 30))
    distant /= len(prices) - 30
    assert adjacent < distant, "no autocorrelation -- series is white noise"


def test_prices_stay_positive(adapter):
    for i in range(0, 400, 7):
        assert adapter.price_on("x", 1000.0, DAY + timedelta(days=i)) > 0


def test_sale_events_persist_across_days(adapter):
    """A sale must last a few days, not flicker on and off daily."""
    flags = [adapter._sale_active("x", DAY + timedelta(days=i)) for i in range(120)]
    if not any(flags):
        pytest.skip("no sale event in this window for this seed")
    # Find the longest run of True; a 3-day bucketing scheme should give >= 2.
    longest = best = 0
    for f in flags:
        best = best + 1 if f else 0
        longest = max(longest, best)
    assert longest >= 2


# ---------------------------------------------------------------- market shape


def test_sellers_differ_in_price():
    prices = {p: SyntheticMarketAdapter(p, seed=99).price_on("x", 50000.0, DAY)
              for p in SELLER_PROFILES}
    assert len(set(prices.values())) == len(SELLER_PROFILES)


def test_discount_is_cheapest_and_express_is_dearest():
    """REGRESSION: independent per-seller walks used to invert this.

    With one random walk per seller, 180 days of divergence (~35%) swamped the
    ~10% archetype spread, so the discount seller was routinely dearer than the
    express one -- seller identity became noise, and a price-weighted ranking
    was ranking nothing. The two-factor model fixes it.

    Only the OUTER ordering is asserted. The two mid-market archetypes are
    allowed to swap: real mid-tier sellers trade places week to week, and
    nothing in the project depends on their relative order. Sales are excluded
    because a deep enough promotion is *supposed* to be able to invert things.
    """
    market = {p: SyntheticMarketAdapter(p, seed=99) for p in SELLER_PROFILES}

    for offset in range(0, 300, 7):
        day = DAY + timedelta(days=offset)
        prices = {name: ad.price_on("x", 50000.0, day, include_sales=False)
                  for name, ad in market.items()}
        assert min(prices, key=prices.get) == "sim_discount", \
            f"discount seller was not cheapest on {day}: {prices}"
        assert max(prices, key=prices.get) == "sim_express", \
            f"express seller was not dearest on {day}: {prices}"


def test_sellers_move_together():
    """Competing sellers of one product face the same costs, so their prices
    must be strongly correlated. Independent walks gave ~zero correlation."""
    days = [DAY + timedelta(days=i) for i in range(120)]
    a = [SyntheticMarketAdapter("sim_discount", seed=99)
         .price_on("x", 50000.0, d, include_sales=False) for d in days]
    b = [SyntheticMarketAdapter("sim_express", seed=99)
         .price_on("x", 50000.0, d, include_sales=False) for d in days]

    mean_a, mean_b = sum(a) / len(a), sum(b) / len(b)
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((y - mean_b) ** 2 for y in b)
    corr = cov / (var_a * var_b) ** 0.5

    assert corr > 0.85, f"sellers are not tracking the market (r={corr:.2f})"


def test_express_seller_is_faster():
    d = SyntheticMarketAdapter("sim_discount", seed=99)
    e = SyntheticMarketAdapter("sim_express", seed=99)
    assert e._delivery_days("x", DAY) < d._delivery_days("x", DAY)


def test_no_seller_wins_every_criterion():
    """The MCDM module needs criteria that genuinely conflict.

    If one seller were cheapest AND fastest AND best-rated, there would be a
    single objectively best offer, every weight vector would pick it, and the
    whole premise of the project would be unsupported by its own fixture.
    """
    market = synthetic_market(seed=99)
    cheapest = min(market, key=lambda a: a.price_on("x", 50000.0, DAY,
                                                   include_sales=False))
    fastest = min(market, key=lambda a: a._delivery_days("x", DAY))
    best_rated = max(market, key=lambda a: a._rating("x"))

    winners = {cheapest.name, fastest.name, best_rated.name}
    assert len(winners) == 3, f"criteria do not conflict: {winners}"


def test_market_has_one_adapter_per_profile():
    market = synthetic_market(seed=5)
    assert len(market) == len(SELLER_PROFILES)
    assert {a.name for a in market} == set(SELLER_PROFILES)


def test_every_listing_is_flagged_synthetic():
    """Non-negotiable: simulated rows must be identifiable forever."""
    for ad in synthetic_market(seed=5):
        assert ad.is_synthetic is True
        for l in ad.listings_on(DAY, "", limit=100):
            assert l.raw["synthetic"] is True
            assert l.source.startswith("sim_")


def test_item_ids_are_stable_across_days():
    """A price series joins on source_item_id -- if it changed per day, every
    observation would look like a brand-new item with one data point."""
    ad = SyntheticMarketAdapter("sim_standard", seed=3)
    ids_a = {l.source_item_id for l in ad.listings_on(DAY, "", limit=100)}
    ids_b = {l.source_item_id for l in ad.listings_on(DAY + timedelta(days=40),
                                                      "", limit=100)}
    # Stock-outs may remove some, so require substantial overlap, not equality.
    assert len(ids_a & ids_b) >= max(1, int(0.7 * min(len(ids_a), len(ids_b))))


def test_stockouts_happen_but_are_not_the_norm():
    ad = SyntheticMarketAdapter("sim_discount", seed=11)
    counts = [len(ad.listings_on(DAY + timedelta(days=i), "", limit=100))
              for i in range(60)]
    assert min(counts) < max(counts), "no stock variation at all"
    assert sum(counts) / len(counts) > 0.5 * max(counts), "too many stock-outs"


def test_query_filter_works():
    ad = SyntheticMarketAdapter("sim_standard", seed=3)
    hits = ad.listings_on(DAY, "iphone", limit=100)
    assert hits
    assert all("iphone" in l.title.lower() for l in hits)


def test_unknown_profile_rejected():
    with pytest.raises(ValueError, match="Unknown profile"):
        SyntheticMarketAdapter("sim_nonexistent")


def test_date_range_is_ordered_and_inclusive():
    end = date(2026, 8, 4)
    days = date_range(5, end=end)
    assert len(days) == 5
    assert days[-1] == end
    assert days[0] == end - timedelta(days=4)
    assert days == sorted(days)
