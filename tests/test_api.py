"""
API contract tests.

These exist so the dashboard and any future client can rely on the response
shape, and so validation genuinely happens at the edge rather than failing
somewhere inside TOPSIS.
"""

import json

import pytest

pytest.importorskip("fastapi", reason="API extras not installed")

from fastapi.testclient import TestClient  # noqa: E402

from src.api.app import app  # noqa: E402
from src.config import _is_secret_field  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------- ops


def test_health_is_reachable(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ok", "degraded")
    assert body["adapters"]
    for key in ("price_history", "cache", "fx", "warnings", "embedder_backend"):
        assert key in body


def test_health_declares_synthetic_sources(client):
    """If results come from a simulation, the API must say so unprompted."""
    body = client.get("/health").json()
    if any(a["synthetic"] for a in body["adapters"]):
        assert body["status"] == "degraded"
        assert any("synthetic" in w for w in body["warnings"])


def test_health_reports_flat_series(client):
    """The diagnostic that would have caught the flat price history."""
    assert "flat_series" in client.get("/health").json()["price_history"]


def test_config_redacts_secrets(client):
    body = client.get("/config").json()
    for key in ("ebay_client_id", "ebay_client_secret"):
        assert body[key] in (None, "***set***")


def test_config_redacts_every_secret_looking_field(client):
    """The previous test named two fields and so proved almost nothing.

    It asserted that the two eBay fields were redacted -- the only two the
    allowlist contained -- so it passed happily while serpapi_key, the single
    credential the project actually uses, was served in full by this endpoint.
    A test that enumerates the implementation cannot detect an omission in it.

    This asserts the PROPERTY instead: nothing whose name looks like a secret
    may leave the process with its value intact. A field added later is covered
    without anyone remembering to extend a list.
    """
    body = client.get("/config").json()
    leaked = [
        name for name, value in body.items()
        if _is_secret_field(name) and value not in (None, "***set***")
    ]
    assert not leaked, f"/config served these secret fields verbatim: {leaked}"


def test_serpapi_key_is_never_served_verbatim(monkeypatch):
    """Named after the bug: GET /config returned the live API key, HTTP 200.

    Asserted against a value this test plants, so it never depends on a real
    key being configured and can never print one in a failure message.
    """
    from dataclasses import replace
    from src import config as config_module

    planted = "planted-secret-value-0123456789"
    patched = replace(config_module.settings, serpapi_key=planted)
    monkeypatch.setattr(config_module, "settings", patched)

    assert planted not in json.dumps(patched.describe())
    assert patched.describe()["serpapi_key"] == "***set***"


def test_openapi_schema_is_generated(client):
    body = client.get("/openapi.json").json()
    assert "/search" in body["paths"]
    assert "/health" in body["paths"]


# ---------------------------------------------------------------- search


def test_search_get_returns_ranked_products(client):
    body = client.get("/search", params={"q": "iphone"}).json()
    assert body["count"] > 0
    product = body["results"][0]
    assert product["num_offers"] >= 1
    assert product["best_offer"]["topsis_score"] >= 0
    # offers must arrive already sorted best-first
    ranks = [o["rank"] for o in product["all_offers"]]
    assert ranks == sorted(ranks)
    scores = [o["score"] for o in product["all_offers"]]
    assert scores == sorted(scores, reverse=True)


def test_search_post_matches_get(client):
    params = {"q": "iphone", "price": 0.9, "delivery_days": 0.05,
              "seller_rating": 0.05}
    got = client.get("/search", params=params).json()
    posted = client.post("/search", json={
        "query": "iphone",
        "weights": {"price": 0.9, "delivery_days": 0.05, "seller_rating": 0.05},
    }).json()
    assert got["count"] == posted["count"]
    assert (got["results"][0]["best_offer"]["source"]
            == posted["results"][0]["best_offer"]["source"])


def test_price_weight_selects_the_cheapest_offer(client):
    """The MCDM module, end to end through HTTP."""
    body = client.get("/search", params={
        "q": "iphone", "price": 1.0, "delivery_days": 0.0, "seller_rating": 0.0,
    }).json()
    product = body["results"][0]
    cheapest = min(o["price"] for o in product["all_offers"])
    assert product["best_offer"]["price"] == cheapest


def test_rating_weight_selects_the_best_rated_offer(client):
    body = client.get("/search", params={
        "q": "iphone", "price": 0.0, "delivery_days": 0.0, "seller_rating": 1.0,
    }).json()
    product = body["results"][0]
    best = max(o["seller_rating"] for o in product["all_offers"])
    assert product["best_offer"]["seller_rating"] == best


def test_weights_are_scale_invariant(client):
    """{8,1,1} and {0.8,0.1,0.1} are the same preference."""
    a = client.get("/search", params={"q": "iphone", "price": 0.8,
                                      "delivery_days": 0.1, "seller_rating": 0.1}).json()
    b = client.get("/search", params={"q": "iphone", "price": 0.08,
                                      "delivery_days": 0.01, "seller_rating": 0.01}).json()
    assert (a["results"][0]["best_offer"]["source"]
            == b["results"][0]["best_offer"]["source"])


def test_vector_normalisation_is_selectable(client):
    """Both modes must be reachable, so the paper's comparison is reproducible
    from the API and not only from a script."""
    r = client.get("/search", params={"q": "iphone", "normalisation": "vector"})
    assert r.status_code == 200
    assert r.json()["normalisation"] == "vector"


def test_search_diagnostics_are_present(client):
    d = client.get("/search", params={"q": "iphone"}).json()["diagnostics"]
    for key in ("listings", "per_source", "cache_hits", "failures", "base_currency"):
        assert key in d


def test_empty_query_returns_the_catalogue(client):
    assert client.get("/search", params={"q": ""}).json()["count"] > 0


def test_nonsense_query_returns_empty_not_error(client):
    body = client.get("/search", params={"q": "zzzzqqqxyznotaproduct"}).json()
    assert body["count"] == 0
    assert body["results"] == []


# ---------------------------------------------------------------- validation


def test_all_zero_weights_rejected(client):
    r = client.get("/search", params={"price": 0, "delivery_days": 0,
                                      "seller_rating": 0})
    assert r.status_code == 422


@pytest.mark.parametrize("params", [
    {"price": 5},                     # above 1
    {"price": -1},                    # below 0
    {"normalisation": "banana"},      # not a known mode
    {"limit_per_source": 0},          # below minimum
    {"limit_per_source": 99999},      # above maximum
])
def test_invalid_parameters_rejected(client, params):
    assert client.get("/search", params=params).status_code == 422


def test_overlong_query_rejected(client):
    assert client.get("/search", params={"q": "x" * 500}).status_code == 422


# ---------------------------------------------------------------- products


def test_products_endpoint_resolves_clusters(client):
    body = client.get("/products", params={"q": ""}).json()
    assert body["clusters"] > 0
    assert body["clusters"] <= body["listings"], "resolution must not invent products"
    assert body["products"][0]["num_offers"] >= 1


# ---------------------------------------------------------------- prices


def test_tracked_items_shape(client):
    items = client.get("/tracked-items", params={"min_observations": 2}).json()
    if not items:
        pytest.skip("no price history in this environment")
    item = items[0]
    for key in ("source", "source_item_id", "observations", "first_seen",
                "last_seen", "min_price", "max_price", "is_synthetic"):
        assert key in item
    # sorted longest-series-first
    counts = [i["observations"] for i in items]
    assert counts == sorted(counts, reverse=True)


def test_price_history_returns_an_ordered_series(client):
    items = client.get("/tracked-items", params={"min_observations": 3}).json()
    if not items:
        pytest.skip("no price history in this environment")
    it = items[0]
    body = client.get(f"/price-history/{it['source']}/{it['source_item_id']}").json()

    assert body["observations"] == len(body["series"])
    dates = [p["observed_on"] for p in body["series"]]
    assert dates == sorted(dates)
    assert body["min_price"] <= body["avg_price"] <= body["max_price"]


def test_missing_price_history_is_404_with_a_pointer(client):
    r = client.get("/price-history/nosuchsource/nosuchitem")
    assert r.status_code == 404
    # The error should tell you where to look next.
    assert "tracked-items" in r.json()["detail"]


def test_response_time_header_present(client):
    assert "X-Response-Time-ms" in client.get("/health").headers
