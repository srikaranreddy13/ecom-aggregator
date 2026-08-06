"""Shared fixtures.

Every test gets an isolated on-disk SQLite file rather than the project's real
`data/aggregator.db`. Tests that wrote to the real database would pollute the
price history -- the one asset in this project that cannot be regenerated.

The DB is a file, not `:memory:`, because the API hands out one connection per
thread and separate in-memory connections do not share data.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Force the fast backend before anything imports src.config, so the whole suite
# runs without a model download.
import os  # noqa: E402

os.environ.setdefault("EMBEDDER_BACKEND", "tfidf")

# Pin the catalogue to the bundled file. Tests must never depend on a public API
# being reachable: a network blip would turn the whole suite red for reasons that
# have nothing to do with the code, and CI would be non-deterministic. The live
# path is exercised by its own tests, which skip when offline.
os.environ.setdefault("CATALOGUE_SOURCE", "file")

# Force the no-key mode. `setdefault` is NOT enough here: a developer's .env sets
# SOURCE=serpapi, and .env is read by src.config, so without an explicit override
# the suite would spend real SerpApi credits -- 250 per MONTH, so a few full runs
# would exhaust the plan and the failure would look like a broken adapter rather
# than an empty wallet. Tests must never consume a metered external quota.
os.environ["SOURCE"] = "shop"
os.environ.pop("SERPAPI_KEY", None)

from src.matching.embedder import Embedder  # noqa: E402
from src.storage import db  # noqa: E402


@pytest.fixture
def temp_db(tmp_path):
    """A throwaway database with the current schema."""
    conn = db.connect(tmp_path / "test.db")
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def embedder():
    """One embedder for the whole session -- constructing it is the slow part."""
    return Embedder(prefer="tfidf")
