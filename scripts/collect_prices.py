"""
Daily price collector.

WHAT WAS WRONG BEFORE. This script collected from `MockAdapter`, which reads a
static JSON file. It therefore wrote the *same price* into price_history every
single day. Running it for six months would have produced a perfectly flat
series and an unforecastable dataset -- while the row count grew and made the
data look healthy. That is the worst kind of bug: silent, and only discovered
at the point you need the data.

WHAT IT DOES NOW. Source selection is explicit and ordered by preference:

    1. eBay live API      -- if EBAY_CLIENT_ID / EBAY_CLIENT_SECRET are set
    2. synthetic market   -- deterministic simulation with real price movement
    3. (never MockAdapter) -- a static file is not a price source

Prices are converted to the base currency before storage, so a series is not a
mix of USD and INR points.

Schedule it daily.

    Windows Task Scheduler:
        Program : C:\\path\\to\\python.exe
        Args    : -m scripts.collect_prices
        Start in: C:\\path\\to\\ecom-aggregator
        Trigger : Daily, 07:00

    Linux cron:
        0 7 * * * cd /path/to/ecom-aggregator && python3 -m scripts.collect_prices >> logs/collect.log 2>&1

To generate history you have already missed, use `scripts/backfill_prices.py`.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.adapters.errors import AdapterError  # noqa: E402
from src.adapters.synthetic_adapter import synthetic_market  # noqa: E402
from src.config import settings  # noqa: E402
from src.logging_setup import configure_logging, get_logger  # noqa: E402
from src.money import normalise_listings  # noqa: E402
from src.storage import db  # noqa: E402

log = get_logger("scripts.collect_prices")

# The basket you track daily. Keep it stable -- changing it starts a new series
# for anything you add and orphans anything you remove.
WATCHLIST = [
    "iphone 15 128gb",
    "samsung galaxy s24 256gb",
    "sony wh-1000xm5",
    "oneplus nord ce4 lite",
    "airpods pro 2nd generation",
]


def build_adapters(allow_synthetic: bool = True):
    """Live sources first; simulation only as a declared fallback."""
    adapters = []

    if settings.has_ebay_credentials():
        from src.adapters.ebay_adapter import EbayAdapter

        ebay = EbayAdapter()
        if ebay.health():
            adapters.append(ebay)
            log.info("ebay adapter active", extra={"marketplace": settings.ebay_marketplace})
        else:
            log.warning("ebay credentials present but health check failed")
    else:
        log.info("no ebay credentials configured")

    if not adapters and allow_synthetic:
        adapters = synthetic_market()
        log.warning(
            "no live source available -- collecting from the synthetic market. "
            "Rows are tagged is_synthetic=1.",
            extra={"sellers": [a.name for a in adapters]},
        )

    return adapters


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-synthetic", action="store_true",
                    help="fail rather than fall back to the simulated market")
    args = ap.parse_args()

    configure_logging()
    stamp = datetime.now().isoformat(timespec="seconds")
    print(f"[{stamp}] price collection run")

    conn = db.connect()
    adapters = build_adapters(allow_synthetic=not args.no_synthetic)

    if not adapters:
        print("  no usable price source. Configure eBay credentials or drop "
              "--no-synthetic.")
        return 1

    synthetic_only = all(getattr(a, "is_synthetic", False) for a in adapters)
    print(f"  sources: {', '.join(a.name for a in adapters)}"
          f"{'  (SIMULATED)' if synthetic_only else ''}")

    total_new = 0
    for query in WATCHLIST:
        listings = []
        for ad in adapters:
            try:
                listings.extend(ad.search(query, limit=20))
            except AdapterError as exc:
                log.warning("adapter failed on query",
                            extra={"adapter": ad.name, "query": query, "error": str(exc)})
            except Exception:  # noqa: BLE001 -- one query must not abort the run
                log.exception("unexpected adapter error",
                              extra={"adapter": ad.name, "query": query})

        listings = normalise_listings(listings)
        new = db.record_prices(conn, listings)
        total_new += new
        print(f"  {query:<32} {len(listings):>3} listings, {new:>3} new rows")

    stats = db.series_stats(conn)
    print(f"\n  new rows this run    : {total_new}")
    print(f"  total observations   : {stats['observations']}")
    print(f"  distinct days        : {stats['distinct_days']}")
    print(f"  synthetic / real     : {stats['synthetic_observations']} / "
          f"{stats['real_observations']}")
    if stats["flat_series"]:
        print(f"  flat (unforecastable): {stats['flat_series']}  <- these have "
              f"zero price variance")
    if stats["distinct_days"] < 30:
        print(f"  -> {30 - stats['distinct_days']} more days before a real-data "
              f"forecast is meaningful (or run scripts/backfill_prices.py)")

    purged = db.cache_purge_expired(conn)
    if purged:
        log.info("purged expired cache entries", extra={"rows": purged})

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
