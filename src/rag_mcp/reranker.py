"""
Cross-encoder reranking wrapper.

Provides a second-stage reranker that reorders bi-encoder search candidates
using a cross-encoder model. Cross-encoders score a (query, document) pair
jointly (rather than comparing independently computed vectors), which is
more accurate but too slow to run over an entire collection — so it's only
applied to a small over-fetched candidate set from the vector search stage.

Note: sentence_transformers is imported lazily (inside load()) to match the
lazy-import pattern used by EmbeddingGenerator, and to avoid paying the import
cost when reranking is disabled.
"""

import sys
import time


class Reranker:
    """Reranks (query, document) candidates using a local CrossEncoder model."""

    def __init__(self, model_name: str):
        self.model_name = model_name
        self.model = None  # CrossEncoder instance, loaded lazily
        self.load_time_ms: float = 0

    def load(self) -> None:
        """Load the model. Imports sentence_transformers here (lazy). Logs time to stderr."""
        from sentence_transformers import CrossEncoder

        start = time.time()
        self.model = CrossEncoder(self.model_name)
        self.load_time_ms = (time.time() - start) * 1000
        print(
            f"[startup] Reranker model loaded in {self.load_time_ms:.0f}ms: {self.model_name}",
            file=sys.stderr,
        )

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        """Score each document against the query. Returns one score per document
        (higher = more relevant). Caller is responsible for sorting/truncating."""
        if not self.model:
            raise RuntimeError("Reranker model not loaded. Call load() first.")
        if not documents:
            return []
        pairs = [(query, doc) for doc in documents]
        scores = self.model.predict(pairs, show_progress_bar=False)
        return scores.tolist()
