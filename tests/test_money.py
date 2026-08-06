"""
Currency normalisation tests.

The headline test is `test_usd_listing_no_longer_beats_inr_on_price`, which
reproduces the original bug: without conversion, a $699 listing out-ranked a
Rs.66,999 listing for the same product because TOPSIS compared 699 < 66999.
"""

from datetime import date

import pytest

from src.adapters.base import Listing
from src.money import (
    RateTable,
    UnknownCurrencyError,
    normalise_listing,
    normalise_listings,
    rate_table,
)
from src.ranking.mcdm import topsis

TABLE = RateTable(as_of=date(2026, 8, 1), base="USD",
                  rates={"USD": 1.0, "INR": 87.5, "EUR": 0.92})


def test_same_currency_is_identity():
    assert TABLE.convert(100.0, "INR", "INR") == 100.0


def test_cross_rate_via_base():
    # 87.5 INR == 1 USD, so 8750 INR -> 100 USD
    assert TABLE.convert(8750.0, "INR", "USD") == pytest.approx(100.0)
    assert TABLE.convert(100.0, "USD", "INR") == pytest.approx(8750.0)


def test_round_trip_is_stable():
    original = 66999.0
    there = TABLE.convert(original, "INR", "EUR")
    back = TABLE.convert(there, "EUR", "INR")
    assert back == pytest.approx(original, rel=1e-9)


def test_case_insensitive():
    assert TABLE.convert(1.0, "usd", "inr") == pytest.approx(87.5)


def test_unknown_currency_raises():
    with pytest.raises(UnknownCurrencyError) as exc:
        TABLE.convert(10.0, "XYZ", "INR")
    # The message must say what IS available, or the error is a dead end.
    assert "XYZ" in str(exc.value)
    assert "INR" in str(exc.value)


def test_normalise_listing_preserves_original():
    l = Listing("ebay", "1", "iPhone 15", 699.0, currency="USD")
    out = normalise_listing(l, to="INR", table=TABLE)

    assert out.currency == "INR"
    assert out.price == pytest.approx(699 * 87.5, rel=1e-6)
    # Conversion must be auditable, not destructive.
    assert out.raw["_original_price"] == 699.0
    assert out.raw["_original_currency"] == "USD"
    assert out.raw["_fx_as_of"] == "2026-08-01"
    # and non-mutating
    assert l.price == 699.0 and l.currency == "USD"


def test_unknown_currency_listing_survives_but_is_flagged():
    """A bad row must not crash the query or silently vanish from its cluster --
    and it must not silently WIN one either.

    An unconverted price is not comparable. A 25 DKK jar left unconverted was
    compared as 25 INR and won the price ranking outright. So the listing is kept
    (dropping it shrinks a product's offer list with no trace) but tagged, and
    the pipeline counts the tag into its diagnostics.
    """
    l = Listing("weird", "1", "thing", 100.0, currency="XYZ")
    out = normalise_listing(l, to="INR", table=TABLE)

    assert out.price == 100.0 and out.currency == "XYZ"   # untouched
    assert out.raw["_price_not_comparable"] is True        # but loud
    assert out.raw["_unconvertible_currency"] == "XYZ"
    assert l.raw.get("_price_not_comparable") is None      # original unmutated


def test_usd_listing_no_longer_beats_inr_on_price():
    """THE ORIGINAL BUG.

    Same physical product from two sources in two currencies. $699 is ~61,163
    INR, so the USD listing genuinely IS cheaper -- but only after conversion.
    On the raw floats, 699 < 66999 makes it look 100x cheaper, and it would win
    on price no matter how it was actually priced.
    """
    cheap_inr = Listing("local", "1", "iPhone 15 128GB", 60_000.0, currency="INR",
                        delivery_days=3, seller_rating=4.5)
    pricey_usd = Listing("ebay", "2", "iPhone 15 128GB", 699.0, currency="USD",
                         delivery_days=3, seller_rating=4.5)

    weights = {"price": 0.9, "delivery_days": 0.05, "seller_rating": 0.05}

    # Unconverted: the USD listing wins purely because its number is smaller.
    raw_winner = topsis([cheap_inr, pricey_usd], weights)[0]
    assert raw_winner.listing.source == "ebay"

    # Converted: 699 USD == 61,162.50 INR, which is more than 60,000.
    converted = [normalise_listing(l, to="INR", table=TABLE)
                 for l in (cheap_inr, pricey_usd)]
    fixed_winner = topsis(converted, weights)[0]
    assert fixed_winner.listing.source == "local"
    assert fixed_winner.listing.price == 60_000.0


def test_normalise_listings_batch_mixed():
    """Three amounts of equal value in different currencies must converge.

    Expected values are DERIVED from the live rate table rather than hardcoded.
    An earlier version asserted `== 8750`, computed from the rates that happened
    to be committed at the time -- so `scripts/update_fx_rates` broke the suite,
    which is backwards: refreshing rates is a supported operation and must not
    look like a regression. What is actually being tested is convergence, not a
    particular number.
    """
    table = rate_table()
    one_usd_in = lambda cur: table.convert(1.0, "USD", cur)  # noqa: E731

    listings = [
        Listing("a", "1", "x", 100 * one_usd_in("INR"), currency="INR"),
        Listing("b", "2", "y", 100 * one_usd_in("USD"), currency="USD"),
        Listing("c", "3", "z", 100 * one_usd_in("EUR"), currency="EUR"),
    ]
    out = normalise_listings(listings, to="INR")

    assert all(l.currency == "INR" for l in out)
    expected = round(100 * one_usd_in("INR"))
    assert [round(l.price) for l in out] == [expected] * 3
    assert not any((l.raw or {}).get("_price_not_comparable") for l in out)


def test_shipped_rate_table_loads_and_is_self_consistent():
    """Guards against a malformed data/fx_rates.json reaching main."""
    tbl = rate_table()
    assert tbl.rates[tbl.base] == 1.0
    assert "INR" in tbl.rates and "USD" in tbl.rates
    assert all(v > 0 for v in tbl.rates.values())
    assert isinstance(tbl.age_days(), int)
