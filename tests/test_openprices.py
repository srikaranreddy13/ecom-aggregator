"""
Open Prices adapter tests.

Runs against saved response shapes, no network. The live path has its own test at
the bottom which SKIPS when offline -- a community API being briefly down must
never turn the suite red.
"""

import pytest

from src.openprices import (
    OpenPricesAdapter,
    OpenPricesCatalogue,
    ShopPrice,
)

PRODUCT_ROW = {
    "code": "3017620422003",
    "product_name": "Nutella",
    "brands": "Nutella, Ferrero",
    "quantity": "750 g",
    "image_url": "https://images.openfoodfacts.org/x.jpg",
    "categories_tags": ["en:spreads", "en:hazelnut-spreads"],
    "price_count": 161,
}


# ---------------------------------------------------------------- shop keys


def test_shop_source_key_is_stable_and_slugged():
    shop = ShopPrice(3.65, "EUR", 271, "Auchan Supermarché", "Grenoble",
                     "France", "2026-07-31")
    assert shop.source_key.startswith("shop:")
    assert " " not in shop.source_key
    # Same shop, same key -- a price series has to join across observations.
    again = ShopPrice(4.10, "EUR", 271, "Auchan Supermarché", "Grenoble",
                      "France", "2026-08-02")
    assert shop.source_key == again.source_key


def test_different_shops_get_different_keys():
    a = ShopPrice(1.0, "EUR", 1, "ALDI", "Lyon", "France", "2026-01-01")
    b = ShopPrice(1.0, "EUR", 2, "ALDI", "Paris", "France", "2026-01-01")
    assert a.source_key != b.source_key, "city must disambiguate a chain"


def test_source_key_collapses_repeated_separators():
    shop = ShopPrice(1.0, "EUR", 9, "L'Éléfàn  --  Bio", "Grenoble",
                     "France", "2026-01-01")
    assert "--" not in shop.source_key


# ---------------------------------------------------------------- catalogue


def test_product_row_maps_to_a_product():
    cat = OpenPricesCatalogue()
    p = cat._to_product(PRODUCT_ROW)
    assert p is not None
    assert p.product_id == "op-3017620422003"
    assert "Nutella" in p.title and "750 g" in p.title   # quantity kept in title
    assert p.brand == "Nutella"
    assert p.image_url
    assert "spreads" in p.tags[0]


def test_quantity_is_kept_because_it_discriminates():
    """A 750g and a 1kg jar are different products, and the quantity is the only
    thing in the title that says so. Dropping it would merge them."""
    cat = OpenPricesCatalogue()
    a = cat._to_product({**PRODUCT_ROW, "quantity": "750 g"})
    b = cat._to_product({**PRODUCT_ROW, "code": "999", "quantity": "1 kg"})
    assert a.title != b.title

    from src.matching.attributes import extract
    assert extract(a.title, a.brand).conflicts_with(extract(b.title, b.brand))


def test_rows_without_a_name_are_dropped():
    cat = OpenPricesCatalogue()
    assert cat._to_product({"code": "1", "product_name": ""}) is None
    assert cat._to_product({"product_name": "No code"}) is None


def test_prices_are_marked_real_not_reference():
    """Every other source in this project carries reference prices. These are
    actual observed shop prices, and the flag is how downstream knows."""
    cat = OpenPricesCatalogue()
    assert cat._to_product(PRODUCT_ROW).is_reference_price is False


# ---------------------------------------------------------------- adapter


class _FakeClient:
    def __init__(self):
        self.calls = []

    def search_products(self, query, limit=40):
        self.calls.append(("search", query))
        return [PRODUCT_ROW]

    def prices_for(self, code, limit=40):
        self.calls.append(("prices", code))
        return [
            ShopPrice(2.76, "EUR", 100, "Super U", "Paris", "France", "2026-08-01"),
            ShopPrice(3.65, "EUR", 271, "Auchan", "Grenoble", "France", "2026-07-31"),
            ShopPrice(3.52, "EUR", 300, "Colruyt", "Laneuveville", "Belgique",
                      "2026-06-12"),
        ]


@pytest.fixture
def adapter(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr("src.openprices._client", fake)
    ad = OpenPricesAdapter(catalogue=OpenPricesCatalogue(),
                          max_products=2, prices_per_product=10)
    ad._fake = fake
    return ad


def test_one_adapter_yields_many_sellers(adapter):
    """This adapter is unusual: ONE instance represents MANY shops. That is what
    makes it real price comparison rather than a simulation of it."""
    listings = adapter.search("nutella", limit=10)
    assert len(listings) == 3
    assert len({l.source for l in listings}) == 3


def test_prices_and_currency_come_through(adapter):
    prices = sorted(l.price for l in adapter.search("nutella", limit=10))
    assert prices == [2.76, 3.52, 3.65]
    assert all(l.currency == "EUR" for l in adapter.search("nutella", limit=10))


def test_observation_date_is_preserved(adapter):
    """Prices are dated, which is what makes this usable as real price history."""
    for l in adapter.search("nutella", limit=10):
        assert l.raw["observed_on"]
        assert len(l.raw["observed_on"]) == 10


def test_item_id_includes_the_date(adapter):
    """Two visits to one shop are separate observations, not a collision. Without
    the date the UNIQUE constraint in price_history would silently keep one."""
    ids = {l.source_item_id for l in adapter.search("nutella", limit=10)}
    assert len(ids) == 3
    assert all(l.raw["observed_on"] in l.source_item_id
               for l in adapter.search("nutella", limit=10))


def test_listings_are_not_marked_synthetic(adapter):
    assert adapter.is_synthetic is False
    for l in adapter.search("nutella", limit=10):
        assert l.raw["synthetic"] is False
        assert l.raw["real_price"] is True


def test_delivery_and_rating_are_none_not_invented(adapter):
    """A supermarket shelf has no delivery estimate and no seller rating. Leaving
    them None and letting the ranking impute equally is honest; filling them with
    plausible-looking numbers would fake a multi-criteria comparison."""
    for l in adapter.search("nutella", limit=10):
        assert l.delivery_days is None
        assert l.seller_rating is None


def test_seller_name_identifies_the_actual_shop(adapter):
    names = {l.seller_name for l in adapter.search("nutella", limit=10)}
    assert "Super U, Paris" in names


def test_every_listing_has_a_working_buy_link(adapter):
    for l in adapter.search("nutella", limit=10):
        assert l.url and l.url.startswith("https://prices.openfoodfacts.org/")


def test_limit_is_respected(adapter):
    assert len(adapter.search("nutella", limit=2)) == 2


def test_fanout_is_capped(adapter):
    """Each product costs a request. A query matching 700 products must not fan
    out into hundreds of calls against a free community API."""
    adapter.search("nutella", limit=100)
    assert sum(1 for c in adapter._fake.calls if c[0] == "prices") <= adapter.max_products


# ---------------------------------------------------------------- live


@pytest.mark.parametrize("query", ["nutella"])
def test_live_api_still_has_the_expected_shape(query):
    """Guards against the upstream API changing under us.

    Skips when offline: a community API being briefly down is not a defect in
    this repository, and a suite that goes red for that reason gets ignored.
    """
    from src.adapters.errors import AdapterError
    from src.openprices import _OpenPricesClient

    client = _OpenPricesClient(timeout=15)
    try:
        rows = client.search_products(query, limit=3)
    except (AdapterError, OSError):
        pytest.skip("Open Prices unreachable")

    assert rows, "live search returned nothing"
    for key in ("code", "product_name", "price_count"):
        assert key in rows[0], f"upstream response lost the {key!r} field"
