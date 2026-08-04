"""
Backfill price history from the synthetic market.

THE PROBLEM THIS SOLVES. `collect_prices.py` collected from `MockAdapter`,
which reads a static JSON file, so it recorded the identical price every day.
The price_history table had 30 rows over 2 days with zero variance. Forecasting
on a flat line is not a hard problem, it is an undefined one -- and you would
not have discovered that until you tried to fit a model.

Because SyntheticMarketAdapter is deterministic in (seed, item, date), history
can be generated for any past date and it is the same history you would have
collected had you been running since then. So the forecasting module is
developable today rather than in 30 days' time.

    python -m scripts.backfill_prices --days 120
    python -m scripts.backfill_prices --days 120 --reset
    python -m scripts.backfill_prices --days 30 --dry-run

Rows are written with is_synthetic=1. Keep them separable from real rows for
the rest of the project's life -- see the honesty note in synthetic_adapter.py.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.adapters.synthetic_adapter import date_range, synthetic_market  # noqa: E402
from src.config import settings  # noqa: E402
from src.logging_setup import configure_logging, get_logger  # noqa: E402
from src.money import normalise_listings  # noqa: E402
from src.storage import db  # noqa: E402

log = get_logger("scripts.backfill_prices")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=120,
                    help="how many days of history to generate (default 120)")
    ap.add_argument("--seed", type=int, default=None,
                    help="override SYNTHETIC_SEED; changing it changes the whole series")
    ap.add_argument("--reset", action="store_true",
                    help="delete existing synthetic rows first (real rows are kept)")
    ap.add_argument("--purge-flat", action="store_true",
                    help="delete series that have >1 observation and zero price "
                         "variance -- leftovers from the old static-file collector")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be written, write nothing")
    args = ap.parse_args()

    configure_logging()
    conn = db.connect()

    if args.purge_flat:
        # A series with zero variance carries no information and, worse, is
        # counted as a real observation on /health -- so it reports data you do
        # not have. Listed before deleting so the removal is auditable.
        flat = conn.execute(
            "SELECT source, source_item_id, COUNT(*) n, MIN(price) p "
            "FROM price_history GROUP BY source, source_item_id "
            "HAVING COUNT(*) > 1 AND MIN(price) = MAX(price)"
        ).fetchall()
        if not flat:
            print("no flat series found")
        else:
            print(f"flat series to remove ({len(flat)}):")
            for r in flat:
                print(f"    {r['source']:<12} {r['source_item_id']:<28} "
                      f"{r['n']:>3} rows all at {r['p']:,.2f}")
            if args.dry_run:
                print("  (dry run -- nothing deleted)")
            else:
                removed = conn.execute(
                    "DELETE FROM price_history WHERE (source, source_item_id) IN "
                    "(SELECT source, source_item_id FROM price_history "
                    " GROUP BY source, source_item_id "
                    " HAVING COUNT(*) > 1 AND MIN(price) = MAX(price))"
                ).rowcount
                conn.commit()
                print(f"  removed {removed} rows")
        print()

    if args.reset and not args.dry_run:
        deleted = conn.execute(
            "DELETE FROM price_history WHERE is_synthetic = 1"
        ).rowcount
        conn.commit()
        print(f"removed {deleted} existing synthetic rows (real rows untouched)")

    adapters = synthetic_market(seed=args.seed)
    days = date_range(args.days)

    print(f"generating {args.days} days x {len(adapters)} sellers "
          f"({days[0]} -> {days[-1]}), base currency {settings.base_currency}")

    written = 0
    listings_seen = 0
    for day in days:
        batch = []
        for ad in adapters:
            batch.extend(ad.listings_on(day, query="", limit=1000))
        listings_seen += len(batch)
        if args.dry_run:
            continue
        batch = normalise_listings(batch)
        written += db.record_prices(conn, batch, observed_on=day)

    stats = db.series_stats(conn)
    print()
    print(f"  listings generated   : {listings_seen}")
    print(f"  rows written         : {written}")
    print(f"  total observations   : {stats['observations']}")
    print(f"  distinct days        : {stats['distinct_days']}")
    print(f"  tracked items        : {stats['tracked_items']}")
    print(f"  synthetic / real     : {stats['synthetic_observations']} / "
          f"{stats['real_observations']}")
    print(f"  flat (unforecastable): {stats['flat_series']}")

    if args.dry_run:
        print("\ndry run -- nothing written")
        return 0

    if stats["flat_series"]:
        print("\n  WARNING: some series still have zero price variance. Those "
              "cannot be forecast -- check which source they came from.")

    # Show one series so it is obvious the prices actually move.
    items = db.tracked_items(conn, min_observations=5)
    if items:
        it = items[0]
        series = db.price_series(conn, it["source"], it["source_item_id"])
        print(f"\n  sample series: {it['source']} / {it['source_item_id']}")
        print(f"    {it['title'][:60]}")
        print(f"    {len(series)} points, "
              f"{it['min_price']:.0f} -- {it['max_price']:.0f} "
              f"(spread {(it['max_price'] - it['min_price']) / it['avg_price'] * 100:.1f}% of mean)")
        tail = series[-8:]
        print("    last 8: " + ", ".join(f"{d[5:]}={p:.0f}" for d, p in tail))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
