"""Regression tests. Run: python -m pytest tests/ -v"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.adapters.mock_adapter import MockAdapter
from src.matching.embedder import Embedder
from src.matching.resolver import (
    EntityResolver,
    extract_colour,
    extract_generation,
    extract_model_number,
    extract_storage_gb,
)
from src.ranking.mcdm import topsis


# ---------------- attribute extraction ----------------

def test_storage_variants():
    assert extract_storage_gb("iPhone 15 128GB Blue") == 128
    assert extract_storage_gb("iPhone 15 128 GB Blue") == 128
    assert extract_storage_gb("Laptop 1TB SSD") == 1024
    assert extract_storage_gb("Sony Headphones Black") is None


def test_colour_modifier_does_not_beat_base():
    """Regression: 'Onyx Black' and 'Black' must agree."""
    assert extract_colour("Galaxy S24 (Onyx Black, 256 GB)") == "black"
    assert extract_colour("Galaxy S24 256 GB Black") == "black"
    assert extract_colour("iPhone Graphite") == "graphite"  # modifier alone survives


def test_model_number_trailing_digit():
    """Regression: WH-1000XM5 must not collapse to the same code as XM4."""
    a = extract_model_number("Sony WH-1000XM5 Wireless Headphones")
    b = extract_model_number("Sony WH1000XM5 Bluetooth Headphone")
    c = extract_model_number("Sony WH-1000XM4 Wireless Headphones")
    assert a == b
    assert a != c


def test_ordinal_is_generation_not_model():
    """Regression: '2nd Gen' is a generation, not a model code."""
    t = "Apple AirPods Pro (2nd Generation) with MagSafe Case USB-C"
    assert extract_generation(t) == "2"
    assert extract_model_number(t) != "2nd"


def test_generation_blocks_merge():
    """Different generations of the same line must not merge."""
    from src.adapters.base import Listing
    a = Listing("x", "1", "AirPods Pro 2nd Generation USB-C", 20000, brand="Apple")
    b = Listing("y", "2", "AirPods Pro 3rd Generation USB-C", 24000, brand="Apple")
    clusters = EntityResolver(Embedder()).resolve([a, b])
    assert len(clusters) == 2


# ---------------- entity resolution ----------------

def test_resolution_perfect_on_sample():
    listings = MockAdapter().search("", limit=1000)
    clusters = EntityResolver(Embedder()).resolve(listings)
    assert len(clusters) == 7

    keyed = {frozenset(l.key for l in c.listings) for c in clusters}
    assert frozenset({"alpha:A101", "beta:B774", "gamma:G23"}) in keyed


def test_xm4_never_merges_with_xm5():
    listings = [l for l in MockAdapter().search("Sony") ]
    clusters = EntityResolver(Embedder()).resolve(listings)
    for c in clusters:
        titles = " ".join(l.title for l in c.listings)
        assert not ("XM4" in titles and "XM5" in titles)


def test_storage_variants_never_merge():
    listings = MockAdapter().search("iPhone")
    clusters = EntityResolver(Embedder()).resolve(listings)
    for c in clusters:
        caps = {extract_storage_gb(l.title) for l in c.listings}
        assert len(caps) == 1


# ---------------- MCDM ----------------

def test_weights_change_the_winner():
    """The whole point of the MCDM module."""
    listings = [l for l in MockAdapter().search("iPhone")
                if extract_storage_gb(l.title) == 128]
    cheap = topsis(listings, {"price": 0.9, "delivery_days": 0.05,
                              "seller_rating": 0.05})[0]
    fast = topsis(listings, {"price": 0.05, "delivery_days": 0.9,
                             "seller_rating": 0.05})[0]
    trust = topsis(listings, {"price": 0.05, "delivery_days": 0.05,
                              "seller_rating": 0.9})[0]

    assert cheap.listing.price == min(l.price for l in listings)
    assert fast.listing.delivery_days == min(l.delivery_days for l in listings)
    assert trust.listing.seller_rating == max(l.seller_rating for l in listings)


def test_minmax_more_responsive_than_vector():
    """Documents WHY minmax is the default -- this is a paper result.

    Vector normalisation preserves each criterion's coefficient of variation,
    so low-variance criteria (price, which differs a few percent across
    sellers) are suppressed relative to high-variance ones (delivery days).
    Min-max equalises the spans, so the user's weights actually steer the
    outcome. We assert minmax distinguishes strictly more profiles.
    """
    listings = [l for l in MockAdapter().search("iPhone")
                if extract_storage_gb(l.title) == 128]
    profiles = (
        {"price": 0.8, "delivery_days": 0.1, "seller_rating": 0.1},
        {"price": 0.2, "delivery_days": 0.65, "seller_rating": 0.15},
        {"price": 0.2, "delivery_days": 0.15, "seller_rating": 0.65},
    )

    def distinct(norm):
        return len({topsis(listings, w, normalisation=norm)[0].listing.key
                    for w in profiles})

    assert distinct("minmax") == 3     # every profile gets its own winner
    assert distinct("vector") < 3      # textbook TOPSIS collapses them


def test_scores_bounded():
    listings = MockAdapter().search("Samsung")
    for r in topsis(listings):
        assert 0.0 <= r.score <= 1.0
