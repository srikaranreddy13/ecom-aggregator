"""
Semantic entity resolution -- the core contribution of the project.

KEY DESIGN DECISION (defend this in your review):
Pure embedding similarity FAILS on e-commerce titles. "iPhone 15 128GB Blue" and
"iPhone 15 256GB Blue" are ~0.97 cosine similar but are DIFFERENT products.
Embeddings capture topic, not specification.

So we use a two-stage approach:
  Stage 1 -- BLOCKING: extract hard attributes (storage, brand, model number,
             colour) with rules. Listings that disagree on a hard attribute can
             never merge, no matter how similar the text.
  Stage 2 -- SEMANTIC: within each block, cluster by embedding cosine similarity
             using agglomerative clustering with a distance threshold.

This hybrid is what makes the numbers work. Report the ablation (embeddings
alone vs. blocking alone vs. both) in your paper -- that IS your result.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
from sklearn.cluster import AgglomerativeClustering

from ..adapters.base import Listing
from ..config import settings
from .embedder import Embedder

# ---------------------------------------------------------------- attributes

STORAGE_RE = re.compile(r"\b(\d{1,4})\s*(gb|tb)\b", re.I)

# Model codes are alphanumeric runs that contain BOTH letters and digits and are
# long enough to be discriminative: wh1000xm5, s24ultra, xm5, a2643.
# The naive [a-z]+\d+[a-z]{0,3} pattern silently fails on a trailing digit
# ("wh1000xm5"), which merges XM4 with XM5. Do not "simplify" this.
MODEL_NUM_RE = re.compile(r"\b(?=[a-z0-9]*[a-z])(?=[a-z0-9]*\d)([a-z0-9]{3,12})\b", re.I)

# Base colours carry the real signal. Marketing modifiers ("Onyx Black",
# "Midnight Blue") must NOT outrank the base colour, or "Onyx Black" and
# "Black" land in different blocks and never merge.
BASE_COLOURS = {
    "black", "white", "blue", "green", "red", "silver", "gold", "grey", "gray",
    "purple", "pink", "yellow", "orange", "beige", "brown",
}

MODIFIER_COLOURS = {
    "titanium", "graphite", "midnight", "starlight", "onyx", "cream",
    "lavender", "mint", "charcoal", "obsidian", "pearl", "sierra", "phantom",
}

COLOURS = BASE_COLOURS | MODIFIER_COLOURS

NOISE_TOKENS = {
    "smartphone", "mobile", "phone", "5g", "4g", "dual", "sim", "unlocked",
    "with", "and", "the", "new", "latest", "official", "genuine", "sealed",
    "brand", "free", "shipping", "wireless", "bluetooth",
}


def extract_storage_gb(title: str) -> int | None:
    """Return storage in GB, normalising TB -> GB."""
    for value, unit in STORAGE_RE.findall(title):
        v = int(value)
        if unit.lower() == "tb":
            return v * 1024
        if v in (16, 32, 64, 128, 256, 512, 1024):
            return v
    return None


def extract_colour(title: str) -> str | None:
    """Prefer the base colour; fall back to a marketing modifier only if no
    base colour is present ("Graphite" alone still identifies a variant)."""
    toks = re.findall(r"[a-z]+", title.lower())
    modifier = None
    for t in toks:
        if t in BASE_COLOURS:
            return "grey" if t == "gray" else t
        if t in MODIFIER_COLOURS and modifier is None:
            modifier = t
    return modifier


ORDINAL_RE = re.compile(r"\b(\d{1,2})(st|nd|rd|th)\b(?:\s*(?:gen|generation))?", re.I)


def extract_generation(title: str) -> str | None:
    """Product generation from '2nd Gen' / '(3rd Generation)'.

    Kept separate from the model number: '2nd' is an ordinal, not a model code.
    Generation still discriminates products (AirPods Pro 2nd vs 3rd gen are
    different items), so it feeds the must-not-link constraint in its own right.
    """
    m = ORDINAL_RE.search(title)
    return m.group(1) if m else None


def extract_model_number(title: str) -> str | None:
    """Longest alphanumeric token containing both letters and digits.

    Excluded: storage tokens (128gb) and ordinals (2nd), both of which are
    handled by their own extractors and would otherwise be mislabelled here.
    """
    cleaned = re.sub(r"[-_]", "", title.lower())
    cleaned = STORAGE_RE.sub(" ", cleaned)
    cleaned = ORDINAL_RE.sub(" ", cleaned)
    candidates = [
        m.group(1)
        for m in MODEL_NUM_RE.finditer(cleaned)
        if not re.fullmatch(r"\d+(gb|tb)", m.group(1))
    ]
    if not candidates:
        return None
    return max(candidates, key=len)


def normalise_title(title: str) -> str:
    """Strip marketing noise so the embedder sees the discriminative tokens."""
    t = title.lower()
    t = re.sub(r"[\(\)\[\],/|]", " ", t)
    t = re.sub(r"\s+", " ", t)
    toks = [w for w in t.split() if w not in NOISE_TOKENS]
    return " ".join(toks).strip()


# ---------------------------------------------------------------- data model


@dataclass
class ProductCluster:
    """A set of listings judged to be the same physical product."""

    cluster_id: str
    listings: list[Listing] = field(default_factory=list)

    @property
    def canonical_title(self) -> str:
        # longest title tends to be the most complete/specified
        return max((l.title for l in self.listings), key=len)

    @property
    def min_price(self) -> float:
        return min(l.price for l in self.listings)

    @property
    def best_by(self):
        return {
            "price": min(self.listings, key=lambda l: l.price),
            "delivery": min(
                self.listings, key=lambda l: l.delivery_days or 999
            ),
            "rating": max(self.listings, key=lambda l: l.seller_rating or 0),
        }

    def __len__(self) -> int:
        return len(self.listings)


# ---------------------------------------------------------------- resolver


class EntityResolver:
    # TF-IDF char-n-gram similarities are systematically lower than transformer
    # cosine similarities, so the threshold cannot be shared between backends.
    DEFAULT_THRESHOLD = {"transformer": 0.72, "tfidf": 0.40}

    def __init__(
        self,
        embedder: Embedder | None = None,
        similarity_threshold: float | None = None,
        use_blocking: bool = True,
        enforce_model_constraint: bool = True,
    ):
        self.embedder = embedder or Embedder()
        if similarity_threshold is None:
            # An explicit SIMILARITY_THRESHOLD in the environment overrides the
            # per-backend default, so it can be tuned without a code change.
            similarity_threshold = settings.similarity_threshold
        if similarity_threshold is None:
            similarity_threshold = self.DEFAULT_THRESHOLD.get(
                self.embedder.backend, 0.60
            )
        self.similarity_threshold = similarity_threshold
        self.use_blocking = use_blocking
        self.enforce_model_constraint = enforce_model_constraint

    # -------- blocking --------
    @staticmethod
    def _block_key(listing: Listing) -> tuple:
        t = listing.title
        return (
            (listing.brand or "").lower() or None,
            extract_storage_gb(t),
            extract_colour(t),
        )

    # -------- clustering within a block --------
    def _cluster_block(self, listings: list[Listing], prefix: str) -> list[ProductCluster]:
        if len(listings) == 1:
            return [ProductCluster(f"{prefix}-0", list(listings))]

        titles = [normalise_title(l.title) for l in listings]
        vecs = self.embedder.encode(titles)

        # cosine distance = 1 - cosine similarity (vectors are L2-normalised)
        sim = np.clip(vecs @ vecs.T, -1.0, 1.0)
        dist = 1.0 - sim

        # Must-not-link: two listings whose extracted model numbers DISAGREE can
        # never merge. If either is None we stay silent and let semantics decide.
        if self.enforce_model_constraint:
            marks = [
                (extract_model_number(l.title), extract_generation(l.title))
                for l in listings
            ]
            for i in range(len(listings)):
                for j in range(i + 1, len(listings)):
                    conflict = any(
                        a and b and a != b for a, b in zip(marks[i], marks[j])
                    )
                    if conflict:
                        dist[i, j] = dist[j, i] = 2.0  # beyond any threshold

        np.fill_diagonal(dist, 0.0)

        model = AgglomerativeClustering(
            n_clusters=None,
            metric="precomputed",
            linkage="average",
            distance_threshold=1.0 - self.similarity_threshold,
        )
        labels = model.fit_predict(dist)

        buckets: dict[int, list[Listing]] = defaultdict(list)
        for lab, lst in zip(labels, listings):
            buckets[int(lab)].append(lst)
        return [
            ProductCluster(f"{prefix}-{lab}", items)
            for lab, items in sorted(buckets.items())
        ]

    # -------- public --------
    def resolve(self, listings: list[Listing]) -> list[ProductCluster]:
        if not listings:
            return []

        if self.use_blocking:
            blocks: dict[tuple, list[Listing]] = defaultdict(list)
            for l in listings:
                blocks[self._block_key(l)].append(l)
        else:
            blocks = {("all",): list(listings)}

        clusters: list[ProductCluster] = []
        for i, (key, items) in enumerate(blocks.items()):
            clusters.extend(self._cluster_block(items, prefix=f"b{i}"))

        clusters.sort(key=len, reverse=True)
        return clusters
