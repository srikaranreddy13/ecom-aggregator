"""
Coarse brand and category guesses for live retailer titles.

WHY THIS EXISTS. Google Shopping (via SerpApi) returns a title and a price --
nothing structured for brand or category, unlike a catalogue API. Two
recommender signals need exactly those two fields:

    affinity    needs BRAND     (same-brand products)
    substitute  needs CATEGORY  (same-category products)

Without them both stayed permanently dark on live data: every SerpApi listing
carried brand=None, so every "same brand" check failed; every product read
category=None, so the substitute bucket was either empty or (in an earlier,
wrong version) a single placeholder shared by everything in one search.

Same philosophy as the tier/unit extractors in attributes.py: small, named,
testable rules instead of a model, because there is no labelled data to train
one and the project has to run with no internet. And the same asymmetry
applies -- a wrong guess is worse than no guess, because a wrong brand or
category actively misleads a signal rather than merely leaving it silent. Both
functions therefore return None rather than guess under real uncertainty.

CATEGORY SLUGS MATCH THE BUNDLED CATALOGUE ON PURPOSE. catalogue.py already
carries a maintained category-pair table, COMPLEMENT_CATEGORIES, mapping e.g.
"smartphones" -> ["mobile-accessories", "mens-watches", "sunglasses"]. Guessing
into that SAME slug set (rather than inventing a second taxonomy) means that
table works for live, search-driven products too -- see
recommender.Recommender._category_pair_complements.

DELIBERATELY NOT ATTEMPTED: gendered categories (mens-shirts vs womens-dresses,
mens-shoes vs womens-shoes, mens-watches vs womens-watches). Guessing gender
from title text alone is unreliable, and getting it wrong is worse than
getting nothing -- a misgendered suggestion is not a near-miss, it is simply
wrong. Those categories only ever come from a real catalogue source.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------- brand

# A curated set of brand names likely to appear in a retail title, grouped by
# domain for readability only (lookup is a single flat set). Lower-cased; the
# canonical display form is reconstructed with str.title() unless listed in
# _BRAND_DISPLAY below.
#
# Extend this list the same way the unit table in attributes.py is extended:
# append a lower-case entry. False-positive risk here is lower than with tier
# or unit words, because these are proper nouns that rarely double as ordinary
# English words in a shopping title -- "titan" and "puma" are the closest
# things to a risk, and are accepted as a known, documented trade-off rather
# than excluded, since the alternative (no brand at all for two large, common
# brands) is a worse default.
_BRANDS = frozenset({
    # phones / electronics
    "apple", "samsung", "sony", "oneplus", "xiaomi", "redmi", "realme", "oppo",
    "vivo", "nokia", "motorola", "google", "huawei", "honor", "asus", "acer",
    "dell", "hp", "lenovo", "msi", "lg", "panasonic", "philips", "jbl", "bose",
    "sennheiser", "boat", "noise", "canon", "nikon", "fujifilm", "gopro",
    "garmin", "fitbit", "amazfit", "anker", "logitech", "corsair", "razer",
    "jiophone", "infinix", "tecno", "iqoo", "poco", "zebronics", "portronics",
    # apparel / lifestyle
    "nike", "adidas", "puma", "reebok", "levis", "zara", "uniqlo", "gucci",
    "prada", "rolex", "fossil", "titan", "casio", "rayban", "woodland",
    "skechers", "bata", "crocs", "fila", "underarmour",
    # appliances / home
    "whirlpool", "bosch", "siemens", "havells", "prestige", "bajaj", "kent",
    "ikea", "usha", "crompton", "butterfly", "pigeon",
})

# Display form where str.title() would be wrong (an acronym, a mixed-case mark,
# or a name with internal punctuation str.title() mangles).
_BRAND_DISPLAY = {
    "hp": "HP", "lg": "LG", "jbl": "JBL", "msi": "MSI", "boat": "boAt",
    "levis": "Levi's", "rayban": "Ray-Ban", "underarmour": "Under Armour",
}

_WORD_RE = re.compile(r"[a-z]+", re.I)

# Product-line names that imply ONE manufacturer strongly enough to use as a
# brand fallback when the manufacturer's own name is absent from the title.
#
# WHY THIS TIER EXISTS. A resale or refurbished listing routinely drops the
# manufacturer and keeps only the product line -- "iPhone 15 Unlocked",
# "APPLE IPHONE 15 128" with no "Apple" at all is the exception, not "iPhone 15
# Unlocked" with no "Apple". Without this, exactly the listings most in need of
# a brand (so affinity can group them) are the ones that miss it.
#
# Kept separate from _BRANDS, not merged into it, because the risk profile is
# different: these are PRODUCT names that happen to identify a manufacturer,
# so the list must stay short and unambiguous -- "iphone", "macbook" and
# "airpods" could never plausibly mean another company's product, which is
# precisely what makes this tier safe to use as a fallback rather than a first
# choice.
_PRODUCT_LINE_BRAND = {
    "iphone": "Apple", "ipad": "Apple", "macbook": "Apple", "airpods": "Apple",
    "imac": "Apple", "apple watch": "Apple",
    "galaxy": "Samsung",
    "pixel": "Google",
    "thinkpad": "Lenovo", "ideapad": "Lenovo",
    "zenbook": "Asus", "vivobook": "Asus", "rog": "Asus",
    "surface": "Microsoft",
    "kindle": "Amazon",
    "playstation": "Sony", "xperia": "Sony", "bravia": "Sony",
    "xbox": "Microsoft",
    "mi band": "Xiaomi", "redmi note": "Xiaomi",
}


def guess_brand(title: str) -> str | None:
    """First known brand token in reading order, or a product-line fallback.

    Reading order (not "any match") because titles are written brand-first by
    convention -- "Apple iPhone 15 128GB Blue", "Sony WH-1000XM4 Wireless
    Headphone" -- so the first hit is reliably the manufacturer rather than an
    incidental mention further into the title (a case, a compatible model,
    a "works with X" clause).

    Falls back to _PRODUCT_LINE_BRAND only when no direct brand word is
    present, so a title that names its own manufacturer is never overridden.
    """
    if not title:
        return None
    low = title.lower()
    for word in _WORD_RE.findall(low):
        if word in _BRANDS:
            return _BRAND_DISPLAY.get(word, word.title())
    for line, brand in _PRODUCT_LINE_BRAND.items():
        if line in low:
            return brand
    return None


# ---------------------------------------------------------------- category

# (category slug, keyword phrases that justify it). Checked in order, first
# match wins, so a more specific rule must precede a more general one that
# could otherwise shadow it -- there are none that currently conflict, but the
# ordering is kept deliberate rather than alphabetical for when one is added.
#
# Phrases are checked as SUBSTRINGS of the lower-cased, hyphen-normalised
# title, which is adequate for multi-word phrases ("phone case") without
# needing a tokeniser; single ambiguous words carry an explicit exclusion
# instead (see "coffee" below), the same asymmetric-cost reasoning as
# attributes.tier_tokens: a guess invented from an ambiguous word is worse
# than the category staying None.
_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("smartphones", (
        "iphone", "galaxy s", "galaxy a", "galaxy z", "galaxy note",
        "smartphone", "android phone", "redmi", "realme", "oneplus",
        "pixel phone", "moto g", "nokia phone", "iqoo", "poco",
    )),
    ("tablets", ("tablet", "ipad", "galaxy tab")),
    ("laptops", (
        "laptop", "macbook", "notebook pc", "chromebook", "thinkpad",
        "ideapad", "zenbook", "vivobook", "gaming laptop", "ultrabook",
    )),
    ("mobile-accessories", (
        "phone case", "phone cover", "screen protector", "phone charger",
        "power bank", "charging cable", "earphone", "earbuds",
        "bluetooth speaker", "wireless earbuds", "phone stand",
    )),
    ("sunglasses", ("sunglasses", "shades", "uv glasses")),
    ("sports-accessories", (
        "dumbbell", "yoga mat", "treadmill", "cricket bat", "badminton",
        "gym equipment", "resistance band", "skipping rope", "football",
    )),
    ("motorcycle", ("motorcycle", "scooter", "bike helmet", "motorbike")),
    ("vehicle", ("car accessory", "car cover", "tyre", "sedan", " suv ")),
    ("furniture", (
        "sofa", "mattress", "wardrobe", "dining table", "bookshelf",
        "recliner", "bed frame", "office chair",
    )),
    ("kitchen-accessories", (
        "cookware", "kettle", "mixer grinder", "blender", "frying pan",
        "air fryer", "kitchen knife", "pressure cooker", "non-stick pan",
    )),
    ("home-decoration", (
        "wall art", "curtains", "candle holder", "wall decor", "floor rug",
        "cushion cover", "flower vase",
    )),
    ("groceries", (
        "basmati rice", "atta", "masala", "breakfast cereal", "grocery pack",
        "tea powder", "coffee powder", "cooking oil",
    )),
    ("beauty", (
        "lipstick", "foundation makeup", "mascara", "eyeliner",
        "compact powder", "kajal", "makeup kit",
    )),
    ("skin-care", (
        "moisturizer", "moisturiser", "sunscreen", "face wash", "face serum",
        "cleanser", "skincare",
    )),
    ("fragrances", (
        "perfume", "deodorant", "eau de parfum", "eau de toilette",
        "eau de cologne", "body spray", "cologne", "fragrance",
    )),
)


def guess_category(title: str) -> str | None:
    """A category slug from the bundled catalogue's taxonomy, or None.

    Deliberately conservative: electronics and a handful of unambiguous,
    ungendered categories only. See the module docstring for why gendered
    categories are out of scope rather than guessed.
    """
    if not title:
        return None
    text = f" {title.lower().replace('-', ' ')} "
    for category, phrases in _CATEGORY_RULES:
        if any(phrase in text for phrase in phrases):
            return category
    return None
