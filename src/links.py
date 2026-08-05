"""
Buy links.

TWO KINDS OF LINK, AND THE DIFFERENCE MATTERS ETHICALLY AND PRACTICALLY:

  1. VERIFIED OFFER LINK -- `Listing.url`. A URL returned by the source itself,
     pointing at the exact offer whose price we displayed. eBay's Browse API
     gives these. Clicking it lands on that seller's page at that price.

  2. RETAILER SEARCH LINK -- constructed here. `amazon.in/s?k=nike+air+jordan+1`
     is just a URL; building one is not scraping and breaks no terms of service.
     It lands the user on the retailer's real results for that product. What it
     does NOT come with is a price, because we have not fetched one.

WHY WE DO NOT SHOW "Amazon: Rs.70,000". Amazon's terms prohibit scraping, and
their Product Advertising API requires an approved Associates account with
qualifying sales. Flipkart's affiliate programme is closed to new signups. So a
price attributed to Amazon or Flipkart here would be invented -- a fabricated
record of a real company's pricing. That is indefensible in a review and
actively harmful if the app is ever deployed publicly. A search link is honest
and still useful: it takes the user to the real page where the real price is.

Every link carries `kind` so the UI can label it truthfully. A verified offer is
rendered as a price you can click; a search link is rendered as "search on
Amazon". They are never presented as the same thing.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote_plus

from .logging_setup import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class BuyLink:
    retailer: str
    label: str
    url: str
    kind: str          # "verified_offer" | "retailer_search"
    price: float | None = None
    currency: str | None = None
    seller: str | None = None

    @property
    def is_verified(self) -> bool:
        return self.kind == "verified_offer"

    def to_dict(self) -> dict:
        return {
            "retailer": self.retailer,
            "label": self.label,
            "url": self.url,
            "kind": self.kind,
            "verified": self.is_verified,
            "price": self.price,
            "currency": self.currency,
            "seller": self.seller,
        }


# Search-URL templates. Only the query string is interpolated, so nothing here
# can be steered into a different host by listing text.
RETAILERS: dict[str, dict[str, str]] = {
    "amazon_in": {"label": "Amazon.in", "template": "https://www.amazon.in/s?k={q}"},
    "flipkart":  {"label": "Flipkart",  "template": "https://www.flipkart.com/search?q={q}"},
    "amazon_com": {"label": "Amazon.com", "template": "https://www.amazon.com/s?k={q}"},
    "ebay":      {"label": "eBay",      "template": "https://www.ebay.com/sch/i.html?_nkw={q}"},
    "google_shopping": {"label": "Google Shopping",
                        "template": "https://www.google.com/search?tbm=shop&q={q}"},
}

# Which retailers to offer by default, per base currency. An Indian user has no
# use for an amazon.com link and vice versa.
DEFAULT_RETAILERS_BY_CURRENCY = {
    "INR": ["amazon_in", "flipkart", "google_shopping"],
    "USD": ["amazon_com", "ebay", "google_shopping"],
}
_FALLBACK_RETAILERS = ["google_shopping", "ebay"]

# Words that make a search worse rather than better -- marketplace boilerplate
# that no retailer's own product page repeats.
_SEARCH_NOISE = {
    "unlocked", "dual", "sim", "genuine", "sealed", "brand", "new", "official",
    "free", "shipping", "offer", "sale", "combo", "pack", "with", "and", "the",
    "for", "in", "of", "best", "top", "premium", "original", "imported", "set",
}


def search_terms(title: str, brand: str | None = None, max_words: int = 9) -> str:
    """Turn a marketplace title into a good retailer search query.

    Long titles search badly: retailer search engines AND the query, so every
    marketplace-specific word ("Dual SIM", "Sealed") shrinks the result set to
    nothing. Keeping the first few meaningful words plus the brand is what
    actually lands on the right product page.
    """
    words = []
    for raw in title.replace("(", " ").replace(")", " ").replace(",", " ").split():
        w = raw.strip("-–—|/").strip()
        if not w or w.lower() in _SEARCH_NOISE:
            continue
        words.append(w)
        if len(words) >= max_words:
            break

    query = " ".join(words)
    if brand and brand.lower() not in query.lower():
        query = f"{brand} {query}"
    return query.strip()


def retailer_links(
    title: str,
    brand: str | None = None,
    currency: str = "INR",
    retailers: list[str] | None = None,
) -> list[BuyLink]:
    """Legitimate search deep-links for one product."""
    names = retailers or DEFAULT_RETAILERS_BY_CURRENCY.get(
        currency.upper(), _FALLBACK_RETAILERS)
    query = search_terms(title, brand)
    encoded = quote_plus(query)

    out = []
    for name in names:
        spec = RETAILERS.get(name)
        if not spec:
            continue
        out.append(BuyLink(
            retailer=name,
            label=spec["label"],
            url=spec["template"].format(q=encoded),
            kind="retailer_search",
        ))
    return out


def offer_links(listings) -> list[BuyLink]:
    """Verified links for listings that actually carry a URL.

    Sorted cheapest-first: if a user is going to click one link, it should be the
    best offer we genuinely verified.
    """
    out = []
    for l in listings:
        if not getattr(l, "url", None):
            continue
        out.append(BuyLink(
            retailer=l.source,
            label=l.seller_name or l.source,
            url=l.url,
            kind="verified_offer",
            price=l.price,
            currency=l.currency,
            seller=l.seller_name,
        ))
    out.sort(key=lambda b: (b.price if b.price is not None else float("inf")))
    return out


def build_links(
    title: str,
    listings,
    brand: str | None = None,
    currency: str = "INR",
) -> dict:
    """Everything the UI needs to render a buy section for one product.

    `verified` is empty until a live source with real URLs is configured -- the
    synthetic sellers have no web pages, and inventing URLs for them would be
    worse than having none. `search` always works, so there is always something
    clickable.
    """
    verified = offer_links(listings)
    search = retailer_links(title, brand=brand, currency=currency)
    return {
        "verified": [b.to_dict() for b in verified],
        "search": [b.to_dict() for b in search],
        "search_query": search_terms(title, brand),
        "has_verified": bool(verified),
        # Stated explicitly so the frontend never has to infer it, and so the
        # distinction survives into any client written later.
        "note": (
            "Verified links point at a specific offer at the price shown. "
            "Search links open the retailer's own results for this product -- "
            "no price is claimed for them."
        ),
    }
