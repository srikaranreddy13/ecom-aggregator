"""
Storage tests: response cache, price history, migration.

`test_backfill_across_dates_keeps_every_day` is the regression for the bug that
made the whole price-history table worthless -- every row landing on today's
date and being collapsed by the UNIQUE constraint.
"""

from datetime import date, timedelta

from src.adapters.base import Listing
from src.storage import db


def _listing(source="a", item="1", price=100.0, **kw):
    return Listing(source, item, kw.pop("title", "Test Product"), price, **kw)


# ---------------------------------------------------------------- cache


def test_cache_round_trip(temp_db):
    db.cache_put(temp_db, "k", [{"a": 1}])
    assert db.cache_get(temp_db, "k") == [{"a": 1}]


def test_cache_miss_returns_none(temp_db):
    assert db.cache_get(temp_db, "absent") is None


def test_cache_expires(temp_db):
    db.cache_put(temp_db, "k", ["v"])
    assert db.cache_get(temp_db, "k", ttl_seconds=3600) == ["v"]
    assert db.cache_get(temp_db, "k", ttl_seconds=0) is None


def test_cache_overwrite(temp_db):
    db.cache_put(temp_db, "k", ["old"])
    db.cache_put(temp_db, "k", ["new"])
    assert db.cache_get(temp_db, "k") == ["new"]


def test_cache_purge_expired(temp_db):
    db.cache_put(temp_db, "k1", ["v"])
    db.cache_put(temp_db, "k2", ["v"])
    assert db.cache_purge_expired(temp_db, ttl_seconds=0) == 2
    assert db.cache_stats(temp_db)["entries"] == 0


def test_listing_survives_a_cache_round_trip(temp_db):
    """The cache stores dicts, so Listing.to_dict/from_dict must be lossless
    for every field the ranking stage reads."""
    original = _listing(price=1234.5, delivery_days=3, seller_rating=4.7,
                        currency="USD", brand="Apple", url="http://x")
    db.cache_put(temp_db, "k", [original.to_dict()])
    restored = Listing.from_dict(db.cache_get(temp_db, "k")[0])

    for field in ("source", "source_item_id", "title", "price", "currency",
                  "delivery_days", "seller_rating", "brand", "url"):
        assert getattr(restored, field) == getattr(original, field)


def test_from_dict_ignores_unknown_keys():
    """A cache written by an older schema must load, not raise TypeError."""
    l = Listing.from_dict({"source": "a", "source_item_id": "1", "title": "t",
                           "price": 1.0, "some_removed_field": "x"})
    assert l.price == 1.0


# ---------------------------------------------------------------- price history


def test_record_prices_is_idempotent(temp_db):
    listings = [_listing()]
    assert db.record_prices(temp_db, listings) == 1
    assert db.record_prices(temp_db, listings) == 0   # same item, same day
    assert db.series_stats(temp_db)["observations"] == 1


def test_backfill_across_dates_keeps_every_day(temp_db):
    """THE REGRESSION.

    Without an explicit observed_on, all 60 rows landed on today and the UNIQUE
    constraint kept exactly one -- so a 60-day backfill produced a single point
    and the price-history table looked populated while holding no series at all.
    """
    start = date(2026, 1, 1)
    for i in range(60):
        day = start + timedelta(days=i)
        db.record_prices(temp_db, [_listing(price=100.0 + i)], observed_on=day)

    series = db.price_series(temp_db, "a", "1")
    assert len(series) == 60
    assert series[0] == (start.isoformat(), 100.0)
    assert series[-1][1] == 159.0
    assert series == sorted(series)          # ordered by date
    assert db.series_stats(temp_db)["distinct_days"] == 60


def test_adapter_supplied_observed_on_wins(temp_db):
    """A backfilled batch carries its date in raw['observed_on']."""
    l = _listing()
    l.raw["observed_on"] = "2026-03-15"
    db.record_prices(temp_db, [l], observed_on=date(2026, 8, 4))
    assert db.price_series(temp_db, "a", "1")[0][0] == "2026-03-15"


def test_synthetic_rows_are_flagged(temp_db):
    real = _listing(item="real")
    fake = _listing(item="fake")
    fake.raw["synthetic"] = True
    db.record_prices(temp_db, [real, fake])

    stats = db.series_stats(temp_db)
    assert stats["synthetic_observations"] == 1
    assert stats["real_observations"] == 1


def test_flat_series_are_detected(temp_db):
    """This diagnostic is what would have caught the original bug immediately."""
    start = date(2026, 1, 1)
    for i in range(10):
        day = start + timedelta(days=i)
        db.record_prices(temp_db, [_listing(item="flat", price=500.0)], observed_on=day)
        db.record_prices(temp_db, [_listing(item="moving", price=500.0 + i)],
                         observed_on=day)

    assert db.series_stats(temp_db)["flat_series"] == 1


def test_tracked_items_respects_minimum(temp_db):
    start = date(2026, 1, 1)
    for i in range(5):
        db.record_prices(temp_db, [_listing(item="long", price=100.0 + i)],
                         observed_on=start + timedelta(days=i))
    db.record_prices(temp_db, [_listing(item="short")], observed_on=start)

    assert len(db.tracked_items(temp_db, min_observations=1)) == 2
    assert len(db.tracked_items(temp_db, min_observations=5)) == 1
    assert db.tracked_items(temp_db, min_observations=5)[0]["source_item_id"] == "long"


def test_tracked_items_aggregates_correctly(temp_db):
    start = date(2026, 1, 1)
    for i, p in enumerate([100.0, 200.0, 300.0]):
        db.record_prices(temp_db, [_listing(price=p)], observed_on=start + timedelta(days=i))

    item = db.tracked_items(temp_db, min_observations=1)[0]
    assert item["n"] == 3
    assert item["min_price"] == 100.0
    assert item["max_price"] == 300.0
    assert item["avg_price"] == 200.0
    assert item["first_seen"] == "2026-01-01"
    assert item["last_seen"] == "2026-01-03"


def test_series_stats_on_empty_db(temp_db):
    """/health calls this before any collection has run -- it must not crash."""
    stats = db.series_stats(temp_db)
    assert stats["observations"] == 0
    assert stats["first_observed"] is None
    assert stats["flat_series"] == 0


# ---------------------------------------------------------------- migration


def test_v1_database_is_migrated(tmp_path):
    """A checkout with the old DB file must be upgraded, not rejected."""
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(str(path))
    old.executescript("""
        CREATE TABLE listing_cache (
            cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at REAL NOT NULL);
        CREATE TABLE price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
            source_item_id TEXT NOT NULL, title TEXT, price REAL NOT NULL,
            currency TEXT DEFAULT 'INR', observed_on TEXT NOT NULL,
            UNIQUE(source, source_item_id, observed_on));
    """)
    old.execute("INSERT INTO price_history(source, source_item_id, title, price, "
                "currency, observed_on) VALUES ('legacy','1','Old Row',999.0,'INR','2026-01-01')")
    old.commit()
    old.close()

    conn = db.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(price_history)")}
    assert {"delivery_days", "seller_rating", "is_synthetic"} <= cols

    # Pre-existing data survives and defaults to real, not synthetic.
    row = conn.execute("SELECT * FROM price_history WHERE source='legacy'").fetchone()
    assert row["price"] == 999.0
    assert row["is_synthetic"] == 0
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()


def test_connect_is_idempotent(tmp_path):
    path = tmp_path / "x.db"
    for _ in range(3):
        conn = db.connect(path)
        conn.close()
    conn = db.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1
    conn.close()
