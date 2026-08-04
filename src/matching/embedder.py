"""
Title embedding.

Primary path  : sentence-transformers (all-MiniLM-L6-v2) -- real semantic vectors.
Fallback path : TF-IDF over character n-grams -- no model download, no GPU,
                works offline. Useful for CI and for the lab machine that has
                no internet on demo day.

Both expose the same .encode(texts) -> (n, d) float array, L2-normalised, so
cosine similarity is just a dot product.
"""

from __future__ import annotations

import numpy as np

from ..config import settings
from ..logging_setup import get_logger

log = get_logger(__name__)


class Embedder:
    """Wraps whichever backend is available."""

    def __init__(self, model_name: str | None = None, prefer: str | None = None):
        # EMBEDDER_BACKEND=tfidf skips the slow torch import entirely.
        # Useful on demo day and on Python versions without torch wheels.
        if prefer is None:
            prefer = settings.embedder_backend
        model_name = model_name or settings.embedder_model
        self.model_name = model_name
        self.backend = None
        self._model = None
        self._vectorizer = None

        if prefer in ("auto", "transformer"):
            try:
                from sentence_transformers import SentenceTransformer

                self._model = SentenceTransformer(model_name)
                self.backend = "transformer"
            except Exception as exc:
                if prefer == "transformer":
                    raise
                log.info("transformer backend unavailable, falling back to tfidf",
                         extra={"reason": str(exc)[:200]})
        if self.backend is None:
            from sklearn.feature_extraction.text import TfidfVectorizer

            self._vectorizer = TfidfVectorizer(
                analyzer="char_wb", ngram_range=(3, 5), min_df=1
            )
            self.backend = "tfidf"

    def encode(self, texts: list[str]) -> np.ndarray:
        if self.backend == "transformer":
            vecs = self._model.encode(
                texts, convert_to_numpy=True, normalize_embeddings=True
            )
            return vecs.astype(np.float32)

        # tfidf: fit_transform on the batch -- fine for per-query clustering
        mat = self._vectorizer.fit_transform(texts).toarray().astype(np.float32)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return mat / norms

    def __repr__(self) -> str:
        return f"<Embedder backend={self.backend!r}>"
