"""
Tests for the catalogue, buy links and recommender.

The link tests matter most. `src/links.py` encodes an ethical boundary -- a
retailer search link must never carry a price, because no price was fetched from
that retailer -- and a boundary that is only described in a comment is one edit
away from being crossed silently.
"""

import pytest

from src.catalogue import DummyJsonCatalogue, FileCatalogue, Product
from src.links import (
    BuyLink,
    build_links,
    offer_links,
    retailer_links,
    search_terms,
)
from src.recommender import Recommender


# ---------------------------------------------------------------- catalogue


@pytest.fixture(scope="module")
def file_catalogue():
    cat = FileCatalogue()
    cat.all_products()
    return cat


def test_file_catalogue_loads(file_catalogue):
    products = file_catalogue.all_products()
    assert len(products) > 20
    assert all(p.product_id for p in products)
    assert all(p.title for p in products)


def test_file_catalogue_spans_several_categories(file_catalogue):
    """The fixture must not be all electronics, or it cannot demonstrate that
    resolution is domain-agnostic."""
    assert len(file_catalogue.categories()) >= 4


def test_search_requires_every_token(file_catalogue):
    """'blue shirt' must not match every shirt. Each token has to appear
    somewhere, otherwise the aggregator returns the whole catalogue for any
    two-word query."""
    hits = file_catalogue.search("apple iphone")
    assert hits
    for p in hits:
        blob = f"{p.title} {p.brand} {p.category} {' '.join(p.tags)}".lower()
        assert "apple" in blob and "iphone" in blob


def test_search_by_category(file_catalogue):
    cats = file_catalogue.categories()
    hits = file_catalogue.search("", category=cats[0])
    assert hits
    assert all(p.category == cats[0] for p in hits)


def test_empty_query_returns_products(file_catalogue):
    assert file_catalogue.search("", limit=5)


def test_nonsense_query_returns_nothing(file_catalogue):
    assert file_catalogue.search("zzzzqqqxyz") == []


def test_reference_prices_are_flagged(file_catalogue):
    """A catalogue price is not a retailer quote, and the flag is what stops it
    being presented as one downstream."""
    assert all(p.is_reference_price for p in file_catalogue.all_products())


@pytest.mark.parametrize("category,expected", [
    ("laptops", "furniture"),
    ("furniture", "kitchen-accessories"),
])
def test_bad_complement_rules_stay_removed(category, expected):
    """REGRESSION. These two rules produced 'MacBook Pro -> Annibale Colombo Bed'
    and 'Sofa -> Lunch Box'. A rule that is right in the abstract and wrong on
    the data is still wrong."""
    rules = DummyJsonCatalogue.COMPLEMENT_CATEGORIES
    assert expected not in rules.get(category, [])


def test_complement_rules_are_never_self_referential():
    for category, targets in DummyJsonCatalogue.COMPLEMENT_CATEGORIES.items():
        assert category not in targets, f"{category} lists itself as a complement"


# ---------------------------------------------------------------- links


def test_search_terms_strips_marketplace_noise():
    """Long titles search badly: retailers AND the query terms, so every
    marketplace-only word shrinks the result set toward zero."""
    q = search_terms("Apple iPhone 15 128GB Blue Unlocked Dual SIM Sealed Brand New",
                     brand="Apple")
    low = q.lower()
    for noise in ("unlocked", "dual", "sim", "sealed", "brand"):
        assert noise not in low
    assert "iphone" in low


def test_search_terms_adds_a_missing_brand():
    q = search_terms("Galaxy S24 256GB", brand="Samsung")
    assert q.lower().startswith("samsung")


def test_search_terms_does_not_duplicate_the_brand():
    q = search_terms("Samsung Galaxy S24", brand="Samsung")
    assert q.lower().count("samsung") == 1


def test_retailer_links_are_well_formed():
    links = retailer_links("Nike Air Jordan 1 Red", brand="Nike", currency="INR")
    assert links
    for b in links:
        assert b.url.startswith("https://")
        assert " " not in b.url            # must be percent-encoded
        assert b.kind == "retailer_search"


def test_retailer_links_never_carry_a_price():
    """THE ETHICAL BOUNDARY.

    We cannot fetch Amazon or Flipkart prices legitimately -- Amazon prohibits
    scraping and gates its API, Flipkart's affiliate programme is closed. So a
    price on a search link would be invented: a fabricated record of a real
    company's pricing. The type must make that impossible to express by accident.
    """
    for b in retailer_links("Anything At All", brand="Brand"):
        assert b.price is None
        assert b.currency is None
        assert b.is_verified is False


def test_currency_selects_relevant_retailers():
    """An Indian user has no use for an amazon.com link, and vice versa."""
    inr = {b.retailer for b in retailer_links("x", currency="INR")}
    usd = {b.retailer for b in retailer_links("x", currency="USD")}
    assert "amazon_in" in inr and "amazon_in" not in usd
    assert "amazon_com" in usd and "amazon_com" not in inr


def test_unknown_currency_still_returns_links():
    """A missing mapping must degrade to something useful, not to nothing."""
    assert retailer_links("x", currency="XYZ")


class _FakeListing:
    def __init__(self, source, price, url=None, seller_name=None, currency="INR"):
        self.source, self.price, self.url = source, price, url
        self.seller_name, self.currency = seller_name, currency


def test_offer_links_only_include_listings_with_urls():
    listings = [
        _FakeListing("ebay", 100.0, url="https://ebay.com/itm/1"),
        _FakeListing("sim_discount", 90.0),          # simulated: no web page
    ]
    links = offer_links(listings)
    assert len(links) == 1
    assert links[0].kind == "verified_offer"
    assert links[0].price == 100.0


def test_offer_links_are_cheapest_first():
    listings = [
        _FakeListing("a", 300.0, url="https://x/3"),
        _FakeListing("b", 100.0, url="https://x/1"),
        _FakeListing("c", 200.0, url="https://x/2"),
    ]
    assert [b.price for b in offer_links(listings)] == [100.0, 200.0, 300.0]


def test_build_links_reports_no_verified_for_simulated_sellers():
    """Inventing URLs for simulated sellers would be worse than having none."""
    out = build_links("Some Product", [_FakeListing("sim_discount", 99.0)],
                      brand="Brand", currency="INR")
    assert out["has_verified"] is False
    assert out["verified"] == []
    assert out["search"]                      # there is always something clickable
    assert "no price is claimed" in out["note"].lower()


def test_buylink_url_has_no_injected_host():
    """Listing text comes from a remote API. It must only ever reach the query
    string, never steer the link to another host."""
    evil = "x https://evil.example/pwn?a=b"
    for b in retailer_links(evil, brand="Brand"):
        host = b.url.split("/")[2]
        assert host in ("www.amazon.in", "www.flipkart.com", "www.amazon.com",
                        "www.ebay.com", "www.google.com")


# ---------------------------------------------------------------- recommender


def _product(pid, title, category, price, brand=None, complements=(), rating=4.0):
    return Product(product_id=pid, title=title, brand=brand, category=category,
                   reference_price=price, rating=rating,
                   complements=list(complements))


@pytest.fixture
def recommender():
    products = [
        _product("phone", "Phone X", "smartphones", 1000.0, "Acme",
                 complements=["case", "charger", "absurd"]),
        _product("case", "Phone Case", "accessories", 20.0, "Acme"),
        _product("charger", "Fast Charger", "accessories", 30.0, "Acme"),
        _product("absurd", "Diamond Dock", "accessories", 90000.0, "Lux"),
        _product("phone2", "Phone Y", "smartphones", 1200.0, "Acme"),
        _product("phone3", "Phone Z", "smartphones", 900.0, "Other"),
        _product("cheap", "Toy Phone", "smartphones", 5.0, "Toy"),
    ]
    return Recommender(products)


def test_recommends_complements_first(recommender):
    recs = recommender.recommend("phone", k=6)
    assert recs
    assert recs[0].kind == "complement"


def test_never_recommends_itself(recommender):
    assert all(r.product.product_id != "phone"
               for r in recommender.recommend("phone", k=10))


def test_absurdly_priced_complement_is_dropped(recommender):
    """A Rs.90,000 dock for a Rs.1,000 phone is a mis-suggestion, not an upsell."""
    assert "absurd" not in {r.product.product_id
                            for r in recommender.recommend("phone", k=10)}


def test_substitutes_stay_in_a_comparable_price_band(recommender):
    ids = {r.product.product_id for r in recommender.recommend("phone", k=10)}
    assert "cheap" not in ids          # a Rs.5 toy is not an alternative


def test_unknown_anchor_returns_empty(recommender):
    assert recommender.recommend("does-not-exist") == []


def test_results_are_sorted_by_score(recommender):
    scores = [r.score for r in recommender.recommend("phone", k=10)]
    assert scores == sorted(scores, reverse=True)


def test_no_duplicate_products(recommender):
    """A product can qualify as both complement and same-brand; it must appear
    once, scored by its strongest justification."""
    recs = recommender.recommend("phone", k=10)
    ids = [r.product.product_id for r in recs]
    assert len(ids) == len(set(ids))


def test_padding_is_refused_over_relevance(recommender):
    """min_score is a floor, not a suggestion. Three good suggestions beat ten
    with seven duds -- padding is what makes a recommender feel broken."""
    recs = recommender.recommend("phone", k=20, min_score=0.9)
    assert all(r.score >= 0.9 for r in recs)


def test_every_recommendation_carries_a_reason(recommender):
    for r in recommender.recommend("phone", k=6):
        assert r.reason and len(r.reason) > 4
        assert r.kind in ("complement", "substitute", "affinity", "cooccurrence")


def test_cooccurrence_outranks_rules_when_present():
    """Real behaviour must beat a hand-written rule. Until a dataset exists the
    signal is zero, but the blend has to already prefer it."""
    products = [
        _product("a", "A", "cat1", 100.0, "B"),
        _product("b", "B item", "cat1", 110.0, "B"),
        _product("c", "C item", "cat1", 105.0, "B"),
    ]
    plain = Recommender(products)
    with_data = Recommender(products, cooccurrence={("a", "c"): 50})

    top_plain = plain.recommend("a", k=3)
    top_data = with_data.recommend("a", k=3)
    assert top_data[0].product.product_id == "c"
    assert top_data[0].kind == "cooccurrence"
    assert top_data[0].score > max(r.score for r in top_plain)


def test_recommend_for_titles_matches_case_insensitively(recommender):
    assert recommender.recommend_for_titles(["  phone x  "], k=3)
    assert recommender.recommend_for_titles(["no such product"], k=3) == []
