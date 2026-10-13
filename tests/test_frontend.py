"""
Frontend and pipeline-visibility tests.

Two things are protected here:

1. THE STATIC MOUNT MUST NOT SHADOW THE API. The frontend is mounted at "/",
   and Starlette matches routes in registration order -- so a mount registered
   too early swallows every route defined after it. That failure is total (the
   whole API 404s) and completely invisible until something calls the API. The
   parametrised test below is cheap insurance against a future reordering.

2. THE MODAL-OVERLAY REGRESSION. `.modal { display: grid }` overrides the UA
   stylesheet's `[hidden] { display: none }`, so a "hidden" full-viewport
   overlay stayed laid out and swallowed every click on the page. The app
   screenshotted perfectly and was completely unusable. Caught by driving the
   real browser, not by any endpoint test -- so the CSS rule that fixes it is
   asserted here.
"""

import pytest

pytest.importorskip("fastapi", reason="API extras not installed")

from fastapi.testclient import TestClient  # noqa: E402

from src.api.app import app  # noqa: E402
from src.config import ROOT  # noqa: E402

UI_DIR = ROOT / "web" / "app"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------- static files


def test_index_is_served_at_root(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # "deal terminal" is the current on-page brand text (see web/app/index.html).
    # Asserted case-insensitively on the brand word rather than the exact
    # phrase, so a future copy tweak does not require touching this test for
    # no functional reason -- the point of this test is "the page loaded and
    # is not a blank shell", not "the wording is frozen forever".
    assert "aggregator" in r.text.lower()


@pytest.mark.parametrize("path,content_type", [
    ("/style.css", "text/css"),
    ("/app.js", "javascript"),
])
def test_assets_are_served(client, path, content_type):
    r = client.get(path)
    assert r.status_code == 200
    assert content_type in r.headers["content-type"]
    assert len(r.content) > 500


def test_missing_asset_is_404_not_a_directory_listing(client):
    r = client.get("/does-not-exist.css")
    assert r.status_code == 404
    assert "Index of" not in r.text


@pytest.mark.parametrize("path", [
    "/health", "/config", "/search", "/products",
    "/tracked-items", "/openapi.json", "/docs",
])
def test_api_routes_survive_the_root_mount(client, path):
    """If the static mount is ever registered before these, they all 404."""
    assert client.get(path).status_code == 200


# ---------------------------------------------------------------- no CDN


def test_frontend_has_no_external_dependencies():
    """The project must run on a lab machine with no internet.

    A CDN <script>/<link> would work on the dev laptop and fail silently in the
    room where it matters, leaving an unstyled or dead page mid-demo.

    Checked by inspecting what the page actually FETCHES -- src/href values --
    rather than grepping for "http". A plain substring search flags
    `xmlns="http://www.w3.org/2000/svg"`, which is an XML namespace identifier
    and is never fetched. (The first version of this test did exactly that and
    failed on the inline SVG favicon.)
    """
    import re

    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    refs = re.findall(r'(?:src|href)\s*=\s*["\']([^"\']+)["\']', html, re.I)
    assert refs, "no src/href attributes found -- did the parser break?"

    for ref in refs:
        low = ref.lower()
        if low.startswith("data:") or low.startswith("#"):
            continue                       # inline, never fetched
        assert not low.startswith(("http://", "https://", "//")), \
            f"external resource fetched at runtime: {ref!r}"

    # Anchors in the footer point at /docs and /health, which are same-origin.
    assert not re.search(r'<script[^>]+src\s*=\s*["\'](?!app\.js)', html, re.I), \
        "unexpected external script tag"


def test_hidden_modal_cannot_swallow_clicks():
    """REGRESSION: an invisible overlay made the entire page unclickable.

    `.modal { display: grid }` is a class selector and beats the UA stylesheet's
    `[hidden] { display: none }`, so the modal stayed laid out as a fixed,
    full-viewport element over everything. The explicit override must exist AND
    must come after the base rule, or specificity/order puts it back.
    """
    css = (UI_DIR / "style.css").read_text(encoding="utf-8")
    assert ".modal[hidden]" in css, "the [hidden] override is missing"
    assert "display: none" in css.split(".modal[hidden]")[1][:60]
    assert css.index(".modal {") < css.index(".modal[hidden]"), \
        "the override must come after the base .modal rule to win on order"


def test_frontend_does_not_reimplement_ranking():
    """The old dashboard computed TOPSIS in JavaScript -- two implementations of
    one algorithm, which can disagree, and the JS one was what people saw.

    This is a smell test, not a proof: it catches the obvious reintroduction.
    """
    js = (UI_DIR / "app.js").read_text(encoding="utf-8").lower()
    for banned in ("function topsis", "euclidean", "d_best", "idealsolution"):
        assert banned not in js, f"ranking logic {banned!r} leaked into the frontend"
    assert "/search?" in js, "the frontend should be calling the ranking API"


# ---------------------------------------------------------------- /products


def test_products_exposes_extracted_attributes(client):
    """The frontend visualises Stage 1 from these fields."""
    body = client.get("/products", params={"q": "iphone"}).json()
    offer = body["products"][0]["offers"][0]

    assert set(offer["attributes"]) == {
        "brand", "storage_gb", "colour", "model_number", "generation"}
    assert offer["block_key"].count("|") == 2


def test_products_reports_resolver_configuration(client):
    r = client.get("/products").json()["resolver"]
    assert r["block_key_fields"] == ["brand", "storage_gb", "colour"]
    assert r["must_not_link_fields"] == ["model_number", "generation"]
    assert isinstance(r["similarity_threshold"], float)
    assert r["backend"] in ("tfidf", "transformer")


def test_block_key_is_shared_within_a_cluster(client):
    """Every listing in a cluster passed through the same block, by construction.
    If this ever fails, blocking and clustering have come out of step."""
    for product in client.get("/products").json()["products"]:
        keys = {o["block_key"] for o in product["offers"]}
        assert len(keys) == 1, f"cluster {product['cluster_id']} spans blocks {keys}"


def test_storage_variants_get_different_block_keys(client):
    """The 128GB/256GB case: ~97% similar text, different products.

    This is the visual the resolution tab is built to show, so it is worth an
    assertion rather than trusting the screenshot.
    """
    products = client.get("/products", params={"q": "iphone"}).json()["products"]
    by_storage = {}
    for p in products:
        gb = p["offers"][0]["attributes"]["storage_gb"]
        if gb:
            by_storage.setdefault(gb, set()).add(p["offers"][0]["block_key"])

    if len(by_storage) < 2:
        pytest.skip("fixture has only one storage variant")
    all_keys = [k for keys in by_storage.values() for k in keys]
    assert len(set(all_keys)) == len(all_keys), \
        "two storage variants shared a block key -- blocking is not separating them"


def test_model_constraint_splits_xm4_from_xm5(client):
    """The headline regression, visible through the API the frontend uses.

    XM4 and XM5 share brand, storage (none) and colour, so blocking alone puts
    them together. Only the model-number must-not-link constraint separates them.
    """
    products = client.get("/products", params={"q": "sony"}).json()["products"]
    for p in products:
        models = {o["attributes"]["model_number"] for o in p["offers"]}
        models.discard(None)
        assert len(models) <= 1, f"cluster {p['cluster_id']} merged models {models}"

    seen = set()
    for p in products:
        seen |= {o["attributes"]["model_number"] for o in p["offers"]}
    if "wh1000xm4" in seen and "wh1000xm5" in seen:
        # They were both present and neither cluster mixed them: the constraint held.
        assert len(products) >= 2


def test_offers_are_sorted_by_price(client):
    """The frontend renders offers in the order the API returns them."""
    for p in client.get("/products").json()["products"]:
        prices = [o["price"] for o in p["offers"]]
        assert prices == sorted(prices)
