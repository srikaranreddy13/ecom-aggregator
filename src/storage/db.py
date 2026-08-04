"""
SQLite persistence: response cache + price history.

The `price_history` table is the one that matters. It is the only asset in this
project you cannot recreate later -- you cannot scrape the past.

CHANGES FROM THE FIRST VERSION, and why:

  * `record_prices` takes an explicit `observed_on`. Without it, backfilling a
    simulated history was impossible: every row landed on today's date and the
    UNIQUE constraint collapsed them into one. The forecasting module needs a
    series, not a point.

  * `is_synthetic` column. Simulated rows and real rows now live in the same
    table (they have to -- forecasting reads one series), so the table itself
    has to record which is which. Any evaluation filters on it. Without this
    flag there is no way to answer "was that MAE measured on real data?", and
    that is exactly the question a reviewer asks.

  * `schema_version` table, so a checkout with an older DB file gets migrated
    instead of failing on a missing column.

  * Query helpers (`price_series`, `series_stats`, `tracked_items`) that the
    forecasting module and the API both need, rather than ad-hoc SQL scattered
    through scripts.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import date
from pathlib import Path

from ..config import settings
from ..logging_setup import get_logger

log = get_logger(__name__)

DB_PATH = settings.db_path

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS listing_cache (
    cache_key   TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    fetched_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS price_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    source_item_id  TEXT NOT NULL,
    title           TEXT,
    price           REAL NOT NULL,
    currency        TEXT DEFAULT 'INR',
    delivery_days   INTEGER,
    seller_rating   REAL,
    is_synthetic    INTEGER NOT NULL DEFAULT 0,
    observed_on     TEXT NOT NULL,
    UNIQUE(source, source_item_id, observed_on)
);

CREATE INDEX IF NOT EXISTS idx_ph_item
    ON price_history(source, source_item_id, observed_on);

CREATE INDEX IF NOT EXISTS idx_ph_day
    ON price_history(observed_on);
"""

# Columns added after v1 shipped. Applied to an existing DB file in order.
_MIGRATIONS = [
    ("price_history", "delivery_days", "INTEGER"),
    ("price_history", "seller_rating", "REAL"),
    ("price_history", "is_synthetic", "INTEGER NOT NULL DEFAULT 0"),
]


def _existing_columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn) -> None:
    """Add columns a v1 database is missing. Idempotent."""
    applied = []
    for table, column, decl in _MIGRATIONS:
        if not _existing_columns(conn, table):
            continue  # table doesn't exist yet; SCHEMA just created it correctly
        if column not in _existing_columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            applied.append(f"{table}.{column}")

    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
    elif row[0] != SCHEMA_VERSION:
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))

    conn.commit()
    if applied:
        log.info("migrated database schema",
                 extra={"added_columns": applied, "version": SCHEMA_VERSION})


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    target = Path(path) if path is not None else settings.db_path
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# ---------------------------------------------------------------- cache


def cache_get(conn, key: str, ttl_seconds: int | None = None):
    ttl = settings.cache_ttl_seconds if ttl_seconds is None else ttl_seconds
    row = conn.execute(
        "SELECT payload, fetched_at FROM listing_cache WHERE cache_key = ?", (key,)
    ).fetchone()
    if not row:
        return None
    age = time.time() - row["fetched_at"]
    if age > ttl:
        log.debug("cache entry expired", extra={"key": key, "age_seconds": round(age)})
        return None
    log.debug("cache hit", extra={"key": key, "age_seconds": round(age)})
    return json.loads(row["payload"])


def cache_put(conn, key: str, payload) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO listing_cache(cache_key, payload, fetched_at) "
        "VALUES (?, ?, ?)",
        (key, json.dumps(payload), time.time()),
    )
    conn.commit()


def cache_purge_expired(conn, ttl_seconds: int | None = None) -> int:
    """Drop stale cache rows. Worth calling from a maintenance job -- an
    unbounded cache table is a slow leak, not a crash, so nothing reminds you."""
    ttl = settings.cache_ttl_seconds if ttl_seconds is None else ttl_seconds
    cur = conn.execute("DELETE FROM listing_cache WHERE fetched_at < ?",
                       (time.time() - ttl,))
    conn.commit()
    return cur.rowcount


def cache_stats(conn) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) n, MIN(fetched_at) oldest, MAX(fetched_at) newest "
        "FROM listing_cache"
    ).fetchone()
    return {
        "entries": row["n"],
        "oldest_age_seconds": round(time.time() - row["oldest"]) if row["oldest"] else None,
        "newest_age_seconds": round(time.time() - row["newest"]) if row["newest"] else None,
    }


# ---------------------------------------------------------------- prices


def record_prices(conn, listings, observed_on: date | str | None = None) -> int:
    """One observation per item per day. Idempotent -- safe to re-run.

    `observed_on` defaults to today but is explicit so a simulated or replayed
    history can be backfilled across many dates. Without that parameter every
    row collapses onto today and the UNIQUE constraint silently discards the
    series -- which is exactly what was happening before.
    """
    if observed_on is None:
        day = date.today().isoformat()
    elif isinstance(observed_on, date):
        day = observed_on.isoformat()
    else:
        day = str(observed_on)

    n = 0
    for l in listings:
        raw = getattr(l, "raw", None) or {}
        # An adapter-supplied observation date wins, so a backfilled batch keeps
        # each row on the day it was simulated for.
        row_day = raw.get("observed_on", day)
        synthetic = 1 if raw.get("synthetic") else 0
        cur = conn.execute(
            "INSERT OR IGNORE INTO price_history"
            "(source, source_item_id, title, price, currency, delivery_days,"
            " seller_rating, is_synthetic, observed_on) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (l.source, l.source_item_id, l.title, l.price, l.currency,
             l.delivery_days, l.seller_rating, synthetic, row_day),
        )
        n += cur.rowcount
    conn.commit()
    return n


def price_series(conn, source: str, item_id: str) -> list[tuple[str, float]]:
    rows = conn.execute(
        "SELECT observed_on, price FROM price_history "
        "WHERE source = ? AND source_item_id = ? ORDER BY observed_on",
        (source, item_id),
    ).fetchall()
    return [(r["observed_on"], r["price"]) for r in rows]


def tracked_items(conn, min_observations: int = 2) -> list[dict]:
    """Every item with enough history to forecast, longest series first."""
    rows = conn.execute(
        "SELECT source, source_item_id, MAX(title) title, COUNT(*) n, "
        "       MIN(observed_on) first_seen, MAX(observed_on) last_seen, "
        "       MIN(price) min_price, MAX(price) max_price, AVG(price) avg_price, "
        "       MAX(is_synthetic) is_synthetic, MAX(currency) currency "
        "FROM price_history GROUP BY source, source_item_id "
        "HAVING COUNT(*) >= ? ORDER BY n DESC, source, source_item_id",
        (min_observations,),
    ).fetchall()
    return [dict(r) for r in rows]


def series_stats(conn) -> dict:
    """Health of the price-history asset. Surfaced on /health.

    `flat_series` is the number that actually matters: a series with zero
    variance is unforecastable, and that was the state of the entire table
    before the synthetic adapter existed.
    """
    total = conn.execute("SELECT COUNT(*) c FROM price_history").fetchone()["c"]
    days = conn.execute(
        "SELECT COUNT(DISTINCT observed_on) c FROM price_history"
    ).fetchone()["c"]
    items = conn.execute(
        "SELECT COUNT(*) c FROM (SELECT 1 FROM price_history "
        "GROUP BY source, source_item_id)"
    ).fetchone()["c"]
    synthetic = conn.execute(
        "SELECT COUNT(*) c FROM price_history WHERE is_synthetic = 1"
    ).fetchone()["c"]
    span = conn.execute(
        "SELECT MIN(observed_on) a, MAX(observed_on) b FROM price_history"
    ).fetchone()
    flat = conn.execute(
        "SELECT COUNT(*) c FROM (SELECT 1 FROM price_history "
        "GROUP BY source, source_item_id "
        "HAVING COUNT(*) > 1 AND MIN(price) = MAX(price))"
    ).fetchone()["c"]

    return {
        "observations": total,
        "distinct_days": days,
        "tracked_items": items,
        "synthetic_observations": synthetic,
        "real_observations": total - synthetic,
        "first_observed": span["a"],
        "last_observed": span["b"],
        "flat_series": flat,
    }
