"""
Currency normalisation.

WHY THIS EXISTS (this was a live bug, not a nicety):

`Listing.price` is a bare float and TOPSIS compares those floats directly. The
mock sources emit INR; the eBay Browse API emits USD or GBP depending on the
marketplace. With no conversion step, a $699 phone scores as *cheaper* than a
Rs.66,999 phone -- the same physical product -- and every ranking that mixes a
live source with a local one is silently wrong. The bug is invisible while only
the mock adapter is wired up, which is exactly why it needed fixing before the
live adapter goes into service rather than after.

So: conversion happens once, at the pipeline boundary, before anything
downstream sees a price. Ranking, clustering and price history all operate in a
single currency (`settings.base_currency`).

RATE SOURCING. Rates live in `data/fx_rates.json` with the date they were
captured. This is deliberately a static snapshot rather than a live FX API:

  * a ranking must be reproducible -- if rates move between two runs, the same
    query returns a different winner and no result in the paper can be replayed
  * FX APIs are another network dependency that can fail on demo day
  * intraday FX precision is irrelevant when the price spread between sellers
    is several percent

The snapshot's age is exposed via `rate_table_age_days()` and surfaced on the
/health endpoint, so a stale table is visible rather than silent. Refresh it
with `python -m scripts.update_fx_rates` when you want current numbers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

from .config import ROOT, settings
from .logging_setup import get_logger

log = get_logger(__name__)

FX_PATH = ROOT / "data" / "fx_rates.json"

# Fallback used only if data/fx_rates.json is missing, so an unconfigured
# checkout still runs instead of crashing. Units per 1 USD.
_FALLBACK = {
    "as_of": "2026-08-01",
    "base": "USD",
    "rates": {"USD": 1.0, "INR": 87.5, "EUR": 0.92, "GBP": 0.78,
              "AUD": 1.52, "CAD": 1.37, "JPY": 152.0, "SGD": 1.34},
}


class UnknownCurrencyError(ValueError):
    """Raised when a listing carries a currency with no rate in the table."""


@dataclass(frozen=True)
class RateTable:
    as_of: date
    base: str
    rates: dict[str, float]      # units of CUR per 1 unit of `base`

    def convert(self, amount: float, frm: str, to: str) -> float:
        frm, to = frm.upper(), to.upper()
        if frm == to:
            return float(amount)
        for cur in (frm, to):
            if cur not in self.rates:
                raise UnknownCurrencyError(
                    f"No FX rate for {cur!r}. Known: {sorted(self.rates)}. "
                    f"Add it to {FX_PATH.name} or set BASE_CURRENCY to a known one."
                )
        # amount -> base -> target
        return float(amount) / self.rates[frm] * self.rates[to]

    def age_days(self, today: date | None = None) -> int:
        return ((today or date.today()) - self.as_of).days


def _parse(payload: dict) -> RateTable:
    rates = {k.upper(): float(v) for k, v in payload["rates"].items()}
    base = payload.get("base", "USD").upper()
    # The base must be 1.0 in its own table; tolerate a snapshot that omits it.
    rates.setdefault(base, 1.0)
    return RateTable(
        as_of=date.fromisoformat(payload["as_of"]),
        base=base,
        rates=rates,
    )


_table: RateTable | None = None


def rate_table(path: Path | None = None) -> RateTable:
    """Load and memoise the rate snapshot."""
    global _table
    if _table is not None and path is None:
        return _table
    target = path or FX_PATH
    if target.is_file():
        table = _parse(json.loads(target.read_text(encoding="utf-8")))
    else:
        log.warning("fx rate file missing, using built-in fallback", extra={"path": str(target)})
        table = _parse(_FALLBACK)
    if path is None:
        _table = table
    return table


def reset_rate_table() -> None:
    """Drop the memoised table. For tests."""
    global _table
    _table = None


def convert(amount: float, frm: str, to: str | None = None) -> float:
    return rate_table().convert(amount, frm, to or settings.base_currency)


def rate_table_age_days() -> int:
    return rate_table().age_days()


# ------------------------------------------------------------------ listings


def normalise_listing(listing, to: str | None = None, table: RateTable | None = None):
    """Return a copy of `listing` priced in `to`.

    The original price and currency are preserved under
    `raw["_original_price"]` / `raw["_original_currency"]` so the UI can still
    show "$699 (converted)" and so the conversion is auditable rather than
    destructive.

    A listing whose currency has no rate is returned unchanged and logged --
    dropping it would silently shrink a cluster, and crashing would let one bad
    row kill an entire query. It stays visible and comparable within its own
    currency instead.
    """
    target = (to or settings.base_currency).upper()
    src = (listing.currency or target).upper()
    if src == target:
        return listing

    tbl = table or rate_table()
    try:
        new_price = round(tbl.convert(listing.price, src, target), 2)
    except UnknownCurrencyError as exc:
        log.warning(
            "skipping currency conversion for listing",
            extra={"listing": listing.key, "currency": src, "reason": str(exc)},
        )
        return listing

    raw = dict(listing.raw or {})
    raw["_original_price"] = listing.price
    raw["_original_currency"] = src
    raw["_fx_as_of"] = tbl.as_of.isoformat()
    return replace(listing, price=new_price, currency=target, raw=raw)


def normalise_listings(listings: list, to: str | None = None) -> list:
    """Convert a batch, loading the rate table once."""
    if not listings:
        return []
    tbl = rate_table()
    target = (to or settings.base_currency).upper()
    out = [normalise_listing(l, to=target, table=tbl) for l in listings]

    converted = sum(1 for l in out if (l.raw or {}).get("_original_currency"))
    if converted:
        log.info(
            "converted listing prices",
            extra={"converted": converted, "total": len(out),
                   "base_currency": target, "fx_as_of": tbl.as_of.isoformat()},
        )
    return out
