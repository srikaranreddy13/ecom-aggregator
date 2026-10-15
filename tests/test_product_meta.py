"""
Tests for src/matching/product_meta.py -- brand and category guessing for live
retailer titles that carry neither field structurally.

Each guesser gets a true-positive table AND a false-positive guard table,
because the whole design argument for this module (see its docstring) is that
a wrong guess is worse than no guess. A test suite that only checked the true
positives could not catch a regression that made the guessers over-eager.
"""

import pytest

from src.matching.product_meta import guess_brand, guess_category


# ---------------------------------------------------------------- brand


@pytest.mark.parametrize("title,expected", [
    ("Apple iPhone 15 128GB Blue", "Apple"),
    ("Samsung Galaxy S25 Ultra 5G", "Samsung"),
    ("Sony WH-1000XM4 Wireless Headphone", "Sony"),
    ("boAt Airdopes 141 Bluetooth Earbuds", "boAt"),
    ("HP Pavilion Gaming Laptop", "HP"),
    ("LG 55 inch OLED TV", "LG"),
])
def test_guess_brand_direct_match(title, expected):
    assert guess_brand(title) == expected


def test_guess_brand_is_case_insensitive():
    assert guess_brand("APPLE IPHONE 15 128 GB BLUE SMARTPHONE") == "Apple"


def test_guess_brand_picks_the_first_match_in_reading_order():
    """Titles are written brand-first by convention; a later incidental mention
    (a compatible accessory, a 'works with' clause) must not win."""
    assert guess_brand("Generic Phone Case compatible with Apple iPhone 15") == "Apple"


@pytest.mark.parametrize("title", [
    "Generic USB Cable 1 Metre",
    "Unbranded Wireless Mouse",
    "No Name Screen Protector",
])
def test_guess_brand_returns_none_rather_than_a_wrong_guess(title):
    assert guess_brand(title) is None


def test_guess_brand_empty_title():
    assert guess_brand("") is None
    assert guess_brand(None) is None


# -------- product-line fallback (the regression this module exists to fix) --


@pytest.mark.parametrize("title,expected", [
    ("Refurbished iPhone 15", "Apple"),
    ("IPHONE MOBILE - 15 - 128GB - BLACK - James and Co", "Apple"),
    ("iPhone 15 (128GB_Blue)", "Apple"),
    ("Galaxy Tab S9 Refurbished", "Samsung"),
    ("Pixel 8 Pro Unlocked", "Google"),
    ("ThinkPad E14 16GB RAM 512GB SSD", "Lenovo"),
])
def test_guess_brand_falls_back_to_product_line(title, expected):
    """A resale or refurbished listing routinely drops the manufacturer's own
    name and keeps only the product line. REGRESSION THIS COVERS: these exact
    titles were seen in a live query and all returned brand=None, which is what
    kept the affinity signal permanently dark for exactly the listings most in
    need of it."""
    assert guess_brand(title) == expected


def test_guess_brand_prefers_the_named_manufacturer_over_the_product_line():
    """A title stating its own manufacturer must never be overridden by the
    product-line fallback, even though the fallback would also match."""
    assert guess_brand("Apple iPhone 15 Pro Max") == "Apple"


# ---------------------------------------------------------------- category


@pytest.mark.parametrize("title,expected", [
    ("Apple iPhone 15 128GB Blue", "smartphones"),
    ("Samsung Galaxy S25 Ultra 5G - AI Smartphone", "smartphones"),
    ("Lenovo ThinkPad E14 16GB RAM 512GB SSD Laptop", "laptops"),
    ("Apple iPad Air 5th Gen Tablet", "tablets"),
    ("boAt Airdopes 141 True Wireless Earbuds", "mobile-accessories"),
    ("Ray-Ban Aviator Sunglasses UV Protection", "sunglasses"),
    ("Philips Air Fryer HD9252", "kitchen-accessories"),
    ("Nescafe Coffee Powder 200g", "groceries"),
    ("Wooden Office Chair for Study Room", "furniture"),
    ("Jean Paul Gaultier Le Beau Eau de Toilette", "fragrances"),
    ("Denver Hamilton Eau de Cologne", "fragrances"),
])
def test_guess_category_direct_match(title, expected):
    assert guess_category(title) == expected


@pytest.mark.parametrize("title", [
    "Wooden Coffee Table for Living Room",   # must NOT become "groceries"
    "Samsung 55 inch Ultra HD Smart TV",     # no TV slug exists in the taxonomy
    "Puma Speed Cat Sneakers",               # gendered footwear: stays None
    "Titan Men's Analog Watch",              # gendered watches: stays None
    "Generic Phone",                         # too vague to place confidently
])
def test_guess_category_returns_none_rather_than_a_wrong_guess(title):
    """The single most important property of this function. 'Coffee Table' is
    the case that forced the design: a keyword match on the bare word 'coffee'
    would have filed furniture under groceries, and that is a worse outcome
    than reporting no category at all."""
    assert guess_category(title) is None


def test_guess_category_never_returns_a_gendered_slug():
    """Gendered categories only ever come from a real catalogue source -- see
    the module docstring for why guessing gender from text is out of scope."""
    gendered = {"mens-shirts", "mens-shoes", "mens-watches", "womens-dresses",
                "womens-shoes", "womens-bags", "womens-jewellery",
                "womens-watches", "tops"}
    titles = [
        "Puma Running Shoes", "Nike Sports Shoes For Men And Women",
        "Casio Analog Watch", "Floral Summer Dress",
    ]
    for title in titles:
        assert guess_category(title) not in gendered


def test_guess_category_empty_title():
    assert guess_category("") is None
    assert guess_category(None) is None
