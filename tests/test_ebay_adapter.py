"""
eBay adapter tests against a saved response fixture.

This code path had NEVER been executed -- there are no API keys on the dev
machine, so `search()` and `_to_listing()` were completely unverified. The first
real request would have been the first test. Fixtures fix that: the normalisation
logic is exercised with no network and no credentials.

The fixture is a trimmed, anonymised copy of a real Browse API response shape.
"""

import pytest

from src.adapters.ebay_adapter import EbayAdapter
from src.adapters.errors import AdapterAuthError, AdapterBadResponse

# Realistic itemSummaries payload, including the awkward cases.
FIXTURE = {
    "total": 4,
    "itemSummaries": [
        {
            "itemId": "v1|1234567890|0",
            "title": "Apple iPhone 15 128GB Blue Unlocked",
            "price": {"value": "699.00", "currency": "USD"},
            "seller": {"username": "topseller", "feedbackPercentage": "98.5",
                       "feedbackScore": 12000},
            "shippingOptions": [{"maxEstimatedDeliveryDate": "2099-01-05T00:00:00.000Z"}],
            "itemWebUrl": "https://www.ebay.com/itm/1234567890",
            "image": {"imageUrl": "https://i.ebayimg.com/x.jpg"},
            "brand": "Apple",
        },
        {
            # No shipping info and no seller feedback -- both are common.
            "itemId": "v1|2222222222|0",
            "title": "Samsung Galaxy S24 256GB Onyx Black",
            "price": {"value": "749.99", "currency": "USD"},
            "seller": {"username": "othershop"},
        },
        {
            # Malformed delivery date must not abort the batch.
            "itemId": "v1|3333333333|0",
            "title": "Sony WH-1000XM5 Wireless Headphones",
            "price": {"value": "328.00", "currency": "USD"},
            "seller": {"username": "audiostore", "feedbackPercentage": "100.0"},
            "shippingOptions": [{"maxEstimatedDeliveryDate": "not-a-date"}],
        },
        {
            # Missing price block entirely.
            "itemId": "v1|4444444444|0",
            "title": "Mystery Item",
            "seller": {"username": "vague"},
        },
    ],
}


class FakeResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.headers = {}
        self.text = str(data)

    def json(self):
        return self._data


@pytest.fixture
def adapter(monkeypatch):
    ad = EbayAdapter(client_id="test-id", client_secret="test-secret")
    # Skip OAuth; token acquisition is tested separately.
    monkeypatch.setattr(ad, "_get_token", lambda: "fake-token")
    return ad


@pytest.fixture
def stub_search(monkeypatch):
    """Replace the HTTP call with a fixture, capturing the request kwargs."""
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse(FIXTURE)

    monkeypatch.setattr("src.adapters.ebay_adapter.http.get", fake_get)
    return captured


# ---------------------------------------------------------------- parsing


def test_search_parses_the_fixture(adapter, stub_search):
    listings = adapter.search("iphone 15", limit=10)
    # 4 items in, 3 parseable (the one with no price block still parses to 0.0,
    # so all 4 come through -- assert we did not silently lose any).
    assert len(listings) == 4
    assert all(l.source == "ebay" for l in listings)


def test_price_and_currency_are_read(adapter, stub_search):
    first = adapter.search("x")[0]
    assert first.price == 699.00
    assert first.currency == "USD"      # NOT the INR default -- this is the bug
                                       # that src/money.py exists to handle


def test_feedback_percentage_maps_onto_five_star_scale(adapter, stub_search):
    listings = {l.source_item_id: l for l in adapter.search("x")}
    # 98.5% / 20 = 4.925 -> 4.93
    assert listings["v1|1234567890|0"].seller_rating == pytest.approx(4.93, abs=0.01)
    # 100% -> 5.0
    assert listings["v1|3333333333|0"].seller_rating == 5.0


def test_missing_feedback_is_none_not_zero(adapter, stub_search):
    """A seller with no feedback is unknown, not terrible. Zero would make the
    imputation in mcdm.py unnecessary and would unfairly sink new sellers."""
    listings = {l.source_item_id: l for l in adapter.search("x")}
    assert listings["v1|2222222222|0"].seller_rating is None


def test_missing_shipping_is_none(adapter, stub_search):
    listings = {l.source_item_id: l for l in adapter.search("x")}
    assert listings["v1|2222222222|0"].delivery_days is None


def test_malformed_delivery_date_is_tolerated(adapter, stub_search):
    """One bad date must yield None for that field, not lose the listing."""
    listings = {l.source_item_id: l for l in adapter.search("x")}
    item = listings["v1|3333333333|0"]
    assert item.delivery_days is None
    assert item.title.startswith("Sony WH-1000XM5")


def test_delivery_days_is_non_negative(adapter, stub_search):
    listings = {l.source_item_id: l for l in adapter.search("x")}
    days = listings["v1|1234567890|0"].delivery_days
    assert days is not None and days >= 0


def test_missing_price_block_defaults_to_zero(adapter, stub_search):
    listings = {l.source_item_id: l for l in adapter.search("x")}
    assert listings["v1|4444444444|0"].price == 0.0


def test_url_and_image_are_carried_through(adapter, stub_search):
    first = adapter.search("x")[0]
    assert first.url == "https://www.ebay.com/itm/1234567890"
    assert first.image_url == "https://i.ebayimg.com/x.jpg"
    assert first.brand == "Apple"


def test_raw_payload_is_retained(adapter, stub_search):
    """Needed to debug a normalisation bug without re-querying the API."""
    assert adapter.search("x")[0].raw["itemId"] == "v1|1234567890|0"


def test_empty_response_is_not_an_error(adapter, monkeypatch):
    monkeypatch.setattr("src.adapters.ebay_adapter.http.get",
                        lambda url, **kw: FakeResponse({"total": 0}))
    assert adapter.search("nothing") == []


def test_marketplace_header_is_sent(adapter, stub_search):
    adapter.search("x")
    assert stub_search["headers"]["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US"
    assert stub_search["headers"]["Authorization"] == "Bearer fake-token"


def test_limit_is_capped_at_the_api_maximum(adapter, stub_search):
    adapter.search("x", limit=5000)
    assert stub_search["params"]["limit"] == 200


# ---------------------------------------------------------------- auth


def test_missing_credentials_raise_typed_auth_error():
    """Must be AdapterAuthError, not RuntimeError -- the pipeline branches on the
    type to decide whether retrying is worthwhile.

    Credentials are cleared on the instance rather than by patching `settings`,
    which is a frozen dataclass on purpose: config should not be mutable at
    runtime, so a test must not need it to be.
    """
    ad = EbayAdapter(client_id="x", client_secret="y")
    ad.client_id = None
    ad.client_secret = None
    with pytest.raises(AdapterAuthError) as exc:
        ad._get_token()
    assert "EBAY_CLIENT_ID" in str(exc.value)


def test_health_is_false_without_credentials():
    assert EbayAdapter(client_id=None, client_secret=None).health() is False


def test_oauth_response_without_token_is_a_bad_response(monkeypatch):
    monkeypatch.setattr("src.adapters.ebay_adapter.http.post",
                        lambda url, **kw: FakeResponse({"error": "invalid_client"}))
    ad = EbayAdapter(client_id="a", client_secret="b")
    with pytest.raises(AdapterBadResponse, match="no access_token"):
        ad._get_token()


def test_token_is_reused_within_its_lifetime(monkeypatch):
    calls = []

    def fake_post(url, **kw):
        calls.append(url)
        return FakeResponse({"access_token": "tok", "expires_in": 7200})

    monkeypatch.setattr("src.adapters.ebay_adapter.http.post", fake_post)
    ad = EbayAdapter(client_id="a", client_secret="b")
    assert ad._get_token() == "tok"
    assert ad._get_token() == "tok"
    assert len(calls) == 1, "re-authenticated unnecessarily"


def test_expired_token_is_refreshed(monkeypatch):
    calls = []

    def fake_post(url, **kw):
        calls.append(url)
        return FakeResponse({"access_token": f"tok{len(calls)}", "expires_in": 0})

    monkeypatch.setattr("src.adapters.ebay_adapter.http.post", fake_post)
    ad = EbayAdapter(client_id="a", client_secret="b")
    ad._get_token()
    ad._get_token()
    assert len(calls) == 2


def test_adapter_is_marked_cacheable():
    """A live, quota-limited API must go through the response cache."""
    assert EbayAdapter.cacheable is True
