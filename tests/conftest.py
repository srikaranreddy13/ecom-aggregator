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
