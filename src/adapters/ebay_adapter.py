"""
Live adapter for the eBay Browse API.

This is the legitimate live-data path: an official, free, keyed API that returns
genuine multi-seller listings with price, shipping and seller feedback -- which
maps directly onto the MCDM criteria. No scraping, no ToS problem.

Setup:
    1. Register at https://developer.ebay.com  -> create a Production keyset.
    2. export EBAY_CLIENT_ID=...  EBAY_CLIENT_SECRET=...
    3. adapter = EbayAdapter(); adapter.search("iphone 15 128gb")

Rate limit on the free tier is generous (thousands of calls/day) but the
pipeline caches anyway -- see storage/db.py.
"""

import base64
import time

from ..config import settings
from ..logging_setup import get_logger
from . import http
from .base import Listing, SourceAdapter
from .errors import AdapterAuthError, AdapterBadResponse

log = get_logger(__name__)

OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"


class EbayAdapter(SourceAdapter):
    name = "ebay"

    # Live API: responses go through the SQLite response cache so a repeated
    # query or a dashboard refresh does not spend quota.
    cacheable = True

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        marketplace: str | None = None,
        timeout: int | None = None,
    ):
        self.client_id = client_id or settings.ebay_client_id
        self.client_secret = client_secret or settings.ebay_client_secret
        self.marketplace = marketplace or settings.ebay_marketplace
        self.timeout = timeout or settings.http_timeout_seconds
        self._token: str | None = None
        self._token_expiry: float = 0.0

    # ---------- auth ----------
    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        if not (self.client_id and self.client_secret):
            raise AdapterAuthError(
                "EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set. Register at "
                "developer.ebay.com, or run with the synthetic market instead "
                "(see src/adapters/synthetic_adapter.py).",
                source=self.name,
            )
        creds = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        resp = http.post(
            OAUTH_URL,
            source=self.name,
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": SCOPE},
            timeout=self.timeout,
        )
        payload = http.json_body(resp, source=self.name)
        token = payload.get("access_token")
        if not token:
            raise AdapterBadResponse(
                f"OAuth response contained no access_token: keys={sorted(payload)}",
                source=self.name,
            )
        self._token = token
        self._token_expiry = time.time() + int(payload.get("expires_in", 7200))
        log.info("obtained ebay oauth token",
                 extra={"expires_in": payload.get("expires_in")})
        return self._token

    # ---------- normalisation ----------
    @staticmethod
    def _delivery_days(item: dict) -> int | None:
        opts = item.get("shippingOptions") or []
        for opt in opts:
            mx = opt.get("maxEstimatedDeliveryDate")
            if mx:
                try:
                    from datetime import datetime, timezone

                    dt = datetime.fromisoformat(mx.replace("Z", "+00:00"))
                    return max(0, (dt - datetime.now(timezone.utc)).days)
                except ValueError:
                    continue
        return None

    @staticmethod
    def _seller_rating(item: dict) -> float | None:
        pct = (item.get("seller") or {}).get("feedbackPercentage")
        if pct is None:
            return None
        try:
            # map 0-100 feedback % onto the 0-5 scale the rest of the system uses
            return round(float(pct) / 20.0, 2)
        except (TypeError, ValueError):
            return None

    def _to_listing(self, item: dict) -> Listing:
        price = (item.get("price") or {})
        return Listing(
            source=self.name,
            source_item_id=item.get("itemId", ""),
            title=item.get("title", ""),
            price=float(price.get("value", 0.0)),
            currency=price.get("currency", "USD"),
            delivery_days=self._delivery_days(item),
            seller_rating=self._seller_rating(item),
            seller_name=(item.get("seller") or {}).get("username"),
            url=item.get("itemWebUrl"),
            image_url=(item.get("image") or {}).get("imageUrl"),
            brand=item.get("brand"),
            raw=item,
        )

    # ---------- public ----------
    def search(self, query: str, limit: int = 25) -> list[Listing]:
        resp = http.get(
            SEARCH_URL,
            source=self.name,
            headers={
                "Authorization": f"Bearer {self._get_token()}",
                "X-EBAY-C-MARKETPLACE-ID": self.marketplace,
            },
            params={"q": query, "limit": min(limit, 200)},
            timeout=self.timeout,
        )
        payload = http.json_body(resp, source=self.name)
        items = payload.get("itemSummaries") or []

        listings = []
        for item in items:
            try:
                listings.append(self._to_listing(item))
            except (TypeError, ValueError, KeyError) as exc:
                # One malformed item must not lose the other 199.
                log.warning("skipping unparseable ebay item",
                            extra={"item_id": item.get("itemId"), "error": str(exc)})
        log.info("ebay search complete",
                 extra={"query": query, "returned": len(items),
                        "parsed": len(listings), "marketplace": self.marketplace})
        return listings

    def health(self) -> bool:
        try:
            self._get_token()
            return True
        except Exception:
            return False
