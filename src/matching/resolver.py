"""
Semantic entity resolution -- the core contribution of the project.

THE KEY DESIGN DECISION (defend this in your review):
Pure embedding similarity FAILS on e-commerce titles. "iPhone 15 128GB Blue" and
"iPhone 15 256GB Blue" are ~0.97 cosine similar but are DIFFERENT products.
Embeddings capture topic, not specification.

So resolution is two-stage:

  Stage 1 -- BLOCKING (coarse, exact, cheap)
      Partition on brand + colour. Different key => never compared. This is a
      performance and safety mechanism, NOT the precision mechanism.

  Stage 2 -- SEMANTIC CLUSTERING (fine) + MUST-NOT-LINK VETO
      Inside each block, cluster by embedding cosine similarity. Before that, any
      pair whose extracted specifications DISAGREE is pushed beyond the merge
      threshold, so no amount of textual similarity can join them.

WHY BLOCKING IS DELIBERATELY COARSE. Block keys must match exactly, so every
field added to the key multiplies the risk that a listing which merely OMITS that
field gets its own singleton block -- where semantic clustering never runs and
recall silently collapses. Precision belongs in the veto, which stays silent when
a spec is missing (see attributes.Attributes.conflicts_with). That division of
labour is what lets the resolver work on domains it has never seen: phones,
laptops, shirts, shampoo, ceiling fans.

Attribute extraction itself is domain-agnostic -- no category list anywhere. See
src/matching/attributes.py for why, and for the laptop RAM/storage bug that
motivated it.

Report the ablation (embeddings alone vs. blocking alone vs. both) in the paper:
that IS the result.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
from sklearn.cluster import AgglomerativeClustering

from ..adapters.base import Listing
from ..config import settings
from ..logging_setup import get_logger
from . import attributes as attrs
from .attributes import (  # noqa: F401  -- re-exported for callers and tests
    Attributes,
    extract,
    extract_colour,
    extract_generation,
    extract_model_number,
    extract_pack_count,
    extract_quantities,
    extract_size,
    normalise_title,
)
from .embedder import Embedder

log = get_logger(__name__)


def extract_storage_gb(title: str) -> int | None:
    """Storage in GB. Kept as a named helper because it is the single most
    recognisable attribute in this project's evaluation, and several scripts and
    tests refer to it by name.

    Note it returns the LARGEST `gb` quantity in the title, so a laptop's
    "16GB RAM 512GB SSD" yields 512, not 16. The old implementation returned 16.
    """
    gb = attrs.extract_quantities(title).get("gb")
    return int(gb) if gb is not None else None


# ---------------------------------------------------------------- data model


@dataclass
class ProductCluster:
    """A set of listings judged to be the same physical product."""

    cluster_id: str
    listings: list[Listing] = field(default_factory=list)

    @property
    def canonical_title(self) -> str:
        # The longest title tends to be the most completely specified.
        return max((l.title for l in self.listings), key=len)

    @property
    def min_price(self) -> float:
        return min(l.price for l in self.listings)

    @property
    def max_price(self) -> float:
        return max(l.price for l in self.listings)

    @property
    def attributes(self) -> Attributes:
        """Specs for the cluster, taken from its most complete title."""
        return attrs.extract(self.canonical_title)

    @property
    def best_by(self):
        return {
            "price": min(self.listings, key=lambda l: l.price),
            "delivery": min(self.listings, key=lambda l: l.delivery_days or 999),
            "rating": max(self.listings, key=lambda l: l.seller_rating or 0),
        }

    def __len__(self) -> int:
        return len(self.listings)


# ---------------------------------------------------------------- resolver


class EntityResolver:
    # TF-IDF char-n-gram similarities are systematically lower than transformer
    # cosine similarities, so the threshold cannot be shared between backends.
    DEFAULT_THRESHOLD = {"transformer": 0.72, "tfidf": 0.40}

    # Distance assigned to a vetoed pair. Must exceed any possible cosine
    # distance (which maxes at 2.0 for opposed vectors) so no threshold can
    # ever merge them.
    VETO_DISTANCE = 10.0

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
        # Name kept for backwards compatibility; it now governs the whole
        # specification veto, not only model numbers.
        self.enforce_model_constraint = enforce_model_constraint

        # Populated during resolve(), for the API's explanation view.
        self.last_vetoes: list[dict] = []

    # -------- blocking --------

    @staticmethod
    def _block_key(listing: Listing, attributes: Attributes | None = None) -> tuple:
        return attrs.block_key(listing.brand, listing.title, attributes)

    # -------- clustering within a block --------

    def _cluster_block(
        self,
        listings: list[Listing],
        attributes: list[Attributes],
        prefix: str,
    ) -> list[ProductCluster]:
        if len(listings) == 1:
            return [ProductCluster(f"{prefix}-0", list(listings))]

        titles = [attrs.normalise_title(l.title) for l in listings]
        vectors = self.embedder.encode(titles)

        # Cosine distance = 1 - cosine similarity (vectors are L2-normalised).
        similarity = np.clip(vectors @ vectors.T, -1.0, 1.0)
        distance = 1.0 - similarity

        if self.enforce_model_constraint:
            for i in range(len(listings)):
                for j in range(i + 1, len(listings)):
                    reason = attributes[i].conflicts_with(attributes[j])
                    if reason:
                        distance[i, j] = distance[j, i] = self.VETO_DISTANCE
                        self.last_vetoes.append({
                            "a": listings[i].key, "b": listings[j].key,
                            "a_title": listings[i].title, "b_title": listings[j].title,
                            "reason": reason,
                            "text_similarity": round(float(similarity[i, j]), 4),
                        })

        np.fill_diagonal(distance, 0.0)

        model = AgglomerativeClustering(
            n_clusters=None,
            metric="precomputed",
            linkage="average",
            distance_threshold=1.0 - self.similarity_threshold,
        )
        labels = model.fit_predict(distance)

        buckets: dict[int, list[Listing]] = defaultdict(list)
        for label, listing in zip(labels, listings):
            buckets[int(label)].append(listing)
        return [
            ProductCluster(f"{prefix}-{label}", items)
            for label, items in sorted(buckets.items())
        ]

    # -------- public --------

    def resolve(self, listings: list[Listing]) -> list[ProductCluster]:
        if not listings:
            return []

        self.last_vetoes = []

        # Extract once per listing. Attribute extraction is regex-heavy and the
        # veto check is O(n^2) within a block, so re-extracting per comparison
        # would dominate the runtime.
        # Brand is passed so the disjoint-name veto can strip it: it is the token
        # every product from one maker shares, and leaving it in makes a Rolex
        # Datejust look related to a Rolex Submariner.
        attributes = [attrs.extract(l.title, l.brand) for l in listings]

        if self.use_blocking:
            blocks: dict[tuple, list[int]] = defaultdict(list)
            for index, listing in enumerate(listings):
                blocks[self._block_key(listing, attributes[index])].append(index)
        else:
            blocks = {("all",): list(range(len(listings)))}

        clusters: list[ProductCluster] = []
        for block_index, (_key, indices) in enumerate(blocks.items()):
            clusters.extend(self._cluster_block(
                [listings[i] for i in indices],
                [attributes[i] for i in indices],
                prefix=f"b{block_index}",
            ))

        clusters.sort(key=len, reverse=True)

        log.debug("resolution complete",
                  extra={"listings": len(listings), "blocks": len(blocks),
                         "clusters": len(clusters), "vetoes": len(self.last_vetoes)})
        return clusters
