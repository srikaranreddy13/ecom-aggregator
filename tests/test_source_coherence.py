"""
The offer source and the catalogue must agree.

THE BUG THIS FILE EXISTS TO PREVENT. Offers were selected "most-real-first" while
the catalogue was chosen independently. Offers ended up coming from Open Prices
(French grocery shops) while the category chips and the recommender were built
over the DummyJSON catalogue. Consequences, all silent:

  * every category button returned the same products, because the categories
    listed did not exist in the data being served
  * every product reported zero recommendations, because the recommender matched
    titles against a catalogue with nothing on screen in it

Nothing raised. Both halves were individually correct and individually tested.
The application was broken because they disagreed with each other -- which is the
kind of failure a per-module test suite cannot see, so it is asserted here at the
seam.
"""

import pytest

pytest.importorskip("fastapi", reason="API extras not installed")

from fastapi.testclient import TestClient  # noqa: E402

from src.api.app import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_categories_come_from_the_catalogue_serving_the_offers(client):
    """A category chip must describe products the user can actually be shown."""
    from src.api.deps import resources

    listed = {c["name"] for c in client.get("/categories").json()["categories"]}
    actual = {p.category for p in resources.catalogue.all_products() if p.category}
    assert listed == actual


def test_different_categories_return_different_products(client):
    """THE REGRESSION. Both of these returned the same rice and cheese."""
    cats = client.get("/categories").json()["categories"]
    if len(cats) < 2:
        pytest.skip("need at least two categories")

    a, b = cats[0]["name"], cats[-1]["name"]
    first = client.get("/search", params={"q": "", "category": a}).json()
    second = client.get("/search", params={"q": "", "category": b}).json()

    titles_a = [r["product"] for r in first["results"]]
    titles_b = [r["product"] for r in second["results"]]
    assert titles_a, f"category {a} returned nothing"
    assert titles_b, f"category {b} returned nothing"
    assert titles_a != titles_b, (
        f"categories {a!r} and {b!r} returned identical products -- the category "
        f"filter is not reaching the offer source"
    )


def test_category_filter_actually_restricts(client):
    """Every product returned for a category must belong to it."""
    cats = client.get("/categories").json()["categories"]
    target = cats[0]["name"]
    body = client.get("/search", params={"q": "", "category": target}).json()
    assert body["results"]
    for row in body["results"]:
        assert row["category"] == target


def test_search_results_can_be_recommended_against(client):
    """A product on screen must be findable by the recommender.

    This is the seam that broke: search served one catalogue's products and the
    recommender indexed another's, so title lookup never hit.
    """
    body = client.get("/search", params={"q": ""}).json()
    assert body["results"]

    hits = 0
    for row in body["results"][:5]:
        rec = client.get("/recommendations",
                        params={"title": row["product"], "k": 4}).json()
        if rec["count"]:
            hits += 1
    assert hits, ("no product from /search produced any recommendation -- the "
                  "recommender is indexing a different catalogue")


def test_offer_sources_are_consistent_across_products(client):
    """All products should be served by the same seller set. A mixture means two
    adapters are live and the comparison is not apples-to-apples."""
    body = client.get("/search", params={"q": ""}).json()
    seen = set()
    for row in body["results"]:
        seen |= set(row["sources"])
    from src.api.deps import resources
    configured = {a.name for a in resources.adapters}
    # Open Prices emits one source per shop, so allow that expansion.
    if not any(s.startswith("shop:") for s in seen):
        assert seen <= configured


def test_health_reports_the_catalogue_actually_in_use(client):
    from src.api.deps import resources

    listed = client.get("/categories").json()
    assert listed["source"] == resources.catalogue.name
    assert listed["total_products"] == len(resources.catalogue.all_products())
