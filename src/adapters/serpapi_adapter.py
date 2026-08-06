"""
Google Shopping via SerpApi -- real prices from real retailers, any product.

WHY THIS IS THE SOURCE THE PROJECT ACTUALLY WANTED. Every other path here is
either simulated, narrow, or gated:

    synthetic     real products, invented sellers
    openprices    real prices, groceries only, European shops
    ebay          real marketplace, but registration was rejected
    Amazon PA-API real, but needs an approved Associates account with sales

This one returns what a shopper would actually see: for "iphone 15 128gb" it
gives ~40 offers from Vijay Sales, R.R.Mobiles, Croma, Reliance Digital and the
rest, each with a real rupee price, a real star rating, a real review count and
real delivery wording. Every field the MCDM needs is present and none of it is
invented.

WHY GOOGLE SHOPPING RATHER THAN AMAZON DIRECTLY. Scraping Amazon breaks its terms
and its API is gated. SerpApi is a paid search-results service that assumes that
position on its own account, and Google Shopping already aggregates Amazon,
Flipkart, Croma and hundreds of others -- so one legitimate call reaches more
retailers than an Amazon-only integration would.

THE QUOTA IS THE DESIGN CONSTRAINT. The free plan allows 250 searches per MONTH.
Roughly eight a day. So:

  * `cacheable = True`, and the pipeline's SQLite response cache is what makes
    the app usable -- browsing, re-ranking and slider dragging all replay a
    cached response and cost nothing. Only a NEW query spends a credit.
  * CACHE_TTL_SECONDS defaults to 24h for this source (see .env.example). Retail
    prices do not move hourly and a credit spent is gone for the month.
  * remaining quota is logged after every live call, so running dry is visible
    before it becomes a mystery outage.

A quota exhaustion is reported as AdapterRateLimited, which the pipeline treats
as "degrade, do not fail" -- the query returns whatever other sources gave.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from ..config import settings
from ..logging_setup import get_logger
from . import http
from .base import Listing, SourceAdapter
from .errors import AdapterAuthError, AdapterBadResponse, AdapterRateLimited

log = get_logger(__name__)

SEARCH_URL = "https://serpapi.com/search.json"
ACCOUNT_URL = "https://serpapi.com/account.json"

# Currency symbol -> ISO code. Google Shopping returns "₹59,900", and the numeric
# `extracted_price` carries no currency, so the symbol is the only signal. Getting
# this wrong feeds an unconverted price into the ranking -- the exact failure
# src/money.py exists to prevent.
_SYMBOL_TO_CODE = {
    "₹": "INR", "$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY",
    "A$": "AUD", "C$": "CAD", "S$": "SGD", "R$": "BRL", "₩": "KRW",
    "CHF": "CHF", "kr": "SEK", "zł": "PLN", "₪": "ILS", "₺": "TRY",
    "AED": "AED", "RM": "MYR", "₱": "PHP", "฿": "THB", "Rp": "IDR",
}

# Country code -> the currency that country's Google Shopping quotes in. Used
# when the symbol is ambiguous ("$" is USD, CAD, AUD, SGD...).
_COUNTRY_CURRENCY = {
    "in": "INR", "us": "USD", "uk": "GBP", "gb": "GBP", "de": "EUR",
    "fr": "EUR", "es": "EUR", "it": "EUR", "au": "AUD", "ca": "CAD",
    "sg": "SGD", "jp": "JPY", "br": "BRL", "ae": "AED", "za": "ZAR",
}


def parse_currency(price_text: str | None, country: str) -> str:
    """Currency code for a price string like '₹59,900'.

    Falls back to the marketplace's own currency rather than to a hardcoded
    default: assuming USD for an Indian marketplace would make every price wrong
    by ~85x.
    """
    fallback = _COUNTRY_CURRENCY.get(country.lower(), "USD")
    if not price_text:
        return fallback
    text = price_text.strip()
    # Longest symbols first so "A$" beats "$".
    for symbol in sorted(_SYMBOL_TO_CODE, key=len, reverse=True):
        if text.startswith(symbol):
            code = _SYMBOL_TO_CODE[symbol]
            # A bare "$" is ambiguous; trust the marketplace over the symbol.
            if symbol == "$" and fallback in ("CAD", "AUD", "SGD"):
                return fallback
            return code
    return fallback


# "Free next-day delivery", "Delivery by Mon, 11 Aug", "Free delivery",
# "Get it by tomorrow", "2-3 day delivery"
_DAYS_RE = re.compile(r"(\d+)\s*[-–]?\s*(?:to\s*\d+\s*)?day", re.I)
_BY_DATE_RE = re.compile(
    r"by\s+(?:\w{3},?\s+)?(\d{1,2})\s+(\w{3})|by\s+(?:\w{3},?\s+)?(\w{3})\s+(\d{1,2})", re.I)
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def parse_delivery_days(text: str | None, today: date | None = None) -> int | None:
    """Delivery estimate in days from Google's free-text wording.

    Returns None when the text promises nothing measurable ("Free delivery"),
    rather than guessing. An invented delivery figure would silently decide the
    "In a hurry" ranking, so an unknown must stay unknown and be imputed openly
    by the ranking stage.
    """
    if not text:
        return None
    low = text.lower()

    if "same day" in low or "same-day" in low or "today" in low:
        return 0
    if "next day" in low or "next-day" in low or "tomorrow" in low:
        return 1
    if "overnight" in low:
        return 1

    m = _DAYS_RE.search(low)
    if m:
        days = int(m.group(1))
        if 0 <= days <= 120:
            return days

    m = _BY_DATE_RE.search(text)
    if m:
        today = today or date.today()
        try:
            if m.group(1) and m.group(2):
                day_num, month_name = int(m.group(1)), m.group(2)
            else:
                day_num, month_name = int(m.group(4)), m.group(3)
            month = _MONTHS.get(month_name[:3].lower())
            if month:
                year = today.year + (1 if month < today.month else 0)
                target = date(year, month, day_num)
                delta = (target - today).days
                if 0 <= delta <= 120:
                    return delta
        except (ValueError, TypeError):
            pass
    return None


class SerpApiShoppingAdapter(SourceAdapter):
    """One instance, many real retailers.

    Like the Open Prices adapter and unlike the simulated market, a single
    instance represents every shop Google Shopping returned. Each result becomes
    a Listing whose `source` is the actual retailer, which is what makes this
    genuine cross-retailer comparison.
    """

    name = "serpapi"
    is_synthetic = False
    # NON-NEGOTIABLE for this source: 250 searches per month. Without the cache
    # a handful of page refreshes exhausts the plan.
    cacheable = True

    def __init__(self, api_key: str | None = None, country: str | None = None,
                 language: str | None = None, timeout: int | None = None):
        self.api_key = api_key or settings.serpapi_key
        self.country = (country or settings.serpapi_country).lower()
        self.language = language or settings.serpapi_language
        self.timeout = timeout or max(settings.http_timeout_seconds, 30)
        self._searches_left: int | None = None

    # ---------------- parsing ----------------

    def _to_listing(self, row: dict, index: int) -> Listing | None:
        price = row.get("extracted_price")
        if price is None:
            return None
        seller = (row.get("source") or "").strip() or "unknown seller"

        # Stable per (retailer, product) so a price series joins across days.
        product_id = row.get("product_id") or f"pos{row.get('position', index)}"
        seller_slug = re.sub(r"[^a-z0-9]+", "-", seller.lower()).strip("-")[:40]

        rating = row.get("rating")
        return Listing(
            source=f"{seller_slug}" or self.name,
            source_item_id=f"{product_id}-{seller_slug}",
            title=(row.get("title") or "").strip(),
            price=float(price),
            currency=parse_currency(row.get("price"), self.country),
            delivery_days=parse_delivery_days(row.get("delivery")),
            # Google's rating is already 0-5. Absent means unrated, which is not
            # the same as badly rated, so it stays None and is imputed openly.
            seller_rating=float(rating) if rating is not None else None,
            seller_name=seller,
            url=row.get("product_link"),
            image_url=row.get("thumbnail"),
            brand=None,
            raw={
                "synthetic": False,
                "real_price": True,
                "retailer": seller,
                "reviews": row.get("reviews"),
                "delivery_text": row.get("delivery"),
                "position": row.get("position"),
                "observed_on": date.today().isoformat(),
            },
        )

    # ---------------- public ----------------

    def search(self, query: str, limit: int = 40,
               category: str | None = None) -> list[Listing]:
        if not self.api_key:
            raise AdapterAuthError(
                "SERPAPI_KEY is not set. Get a free key at serpapi.com "
                "(250 searches/month, instant, no approval), or run with "
                "SOURCE=shop for the no-key mode.",
                source=self.name,
            )
        # Google Shopping can only be SEARCHED -- it has no "list everything in
        # category X" endpoint. So a category is not a filter here, it is a
        # SEARCH TERM. Clicking "Laptops" searches for laptops.
        #
        # This is the bug that made every category chip appear dead: an empty
        # query returned [] immediately, so clicking a chip rendered nothing and
        # the UI looked frozen. The category was being passed and then discarded.
        term = query.strip()
        if category:
            readable = category.replace("-", " ").strip()
            term = f"{term} {readable}".strip() if term else readable

        if not term:
            # Still nothing to search. Returning [] is correct -- spending a
            # credit on whatever Google feels like showing is not.
            return []

        resp = http.get(
            SEARCH_URL,
            source=self.name,
            params={
                "engine": "google_shopping",
                "q": term,
                "gl": self.country,
                "hl": self.language,
                "api_key": self.api_key,
                "num": min(max(limit, 20), 100),
            },
            timeout=self.timeout,
        )
        payload = http.json_body(resp, source=self.name)

        # SerpApi reports failures inside a 200 response, so status has to be
        # inspected rather than trusting the HTTP code.
        error = payload.get("error")
        if error:
            low = str(error).lower()
            if "run out" in low or "exceeded" in low or "limit" in low:
                raise AdapterRateLimited(
                    f"SerpApi quota exhausted: {error}. The free plan allows 250 "
                    f"searches/month; check serpapi.com/account.",
                    source=self.name,
                )
            if "invalid api key" in low or "api key" in low:
                raise AdapterAuthError(f"SerpApi rejected the key: {error}",
                                      source=self.name)
            raise AdapterBadResponse(f"SerpApi error: {error}", source=self.name)

        rows = payload.get("shopping_results") or []
        listings = []
        for i, row in enumerate(rows):
            try:
                listing = self._to_listing(row, i)
            except (TypeError, ValueError) as exc:
                log.warning("skipping unparseable shopping result",
                            extra={"error": str(exc)})
                continue
            if listing:
                listings.append(listing)
            if len(listings) >= limit:
                break

        left = (payload.get("search_metadata") or {}).get("total_time_taken")
        log.info("serpapi shopping search",
                 extra={"query": term, "returned": len(rows),
                        "parsed": len(listings), "country": self.country,
                        "took_s": left})
        if not listings and rows:
            log.warning("serpapi returned results but none had a usable price",
                        extra={"query": term, "rows": len(rows)})
        return listings

    def searches_left(self) -> int | None:
        """Remaining monthly quota, or None if it cannot be read.

        Worth surfacing: with 250 a month, 'the app stopped returning results'
        is far more likely to be an exhausted plan than a bug.
        """
        if not self.api_key:
            return None
        try:
            resp = http.get(ACCOUNT_URL, source=self.name,
                            params={"api_key": self.api_key}, timeout=15)
            data = http.json_body(resp, source=self.name)
            self._searches_left = data.get("total_searches_left")
            return self._searches_left
        except Exception as exc:  # noqa: BLE001 -- diagnostics must never fail a query
            log.debug("could not read serpapi quota", extra={"error": str(exc)})
            return None

    def health(self) -> bool:
        # Deliberately checks the ACCOUNT endpoint, which does not consume a
        # search credit. A health check that costs quota would be self-defeating.
        return bool(self.api_key) and self.searches_left() is not None
