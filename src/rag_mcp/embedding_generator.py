"""
Embedding generation wrapper for SentenceTransformer models.

Provides a simple interface for loading a local embedding model and encoding
text into vector embeddings for both indexing and querying.

Note: sentence_transformers/torch are imported lazily (inside load()) to avoid
blocking the entire package if PyTorch DLLs are locked or unavailable.
"""

import sys
import time

from rag_mcp import observability


class EmbeddingGenerator:
    """Generates embeddings using a local SentenceTransformer model."""

    def __init__(self, model_name: str, query_instruction: str = ""):
        self.model_name = model_name
        # Instruction prefix prepended to queries only (not documents). Some
        # models (e.g. BAAI/bge-*) need this for good retrieval quality.
        self.query_instruction = query_instruction
        self.model = None  # SentenceTransformer instance, loaded lazily
        self.load_time_ms: float = 0

    def load(self) -> None:
        """Load the model. Imports sentence_transformers here (lazy). Logs time to stderr."""
        from sentence_transformers import SentenceTransformer

        start = time.time()
        self.model = SentenceTransformer(self.model_name)
        self.load_time_ms = (time.time() - start) * 1000
        print(
            f"[startup] Embedding model loaded in {self.load_time_ms:.0f}ms: {self.model_name}",
            file=sys.stderr,
        )

    def encode(self, texts: list[str], batch_size: int = 256) -> list[list[float]]:
        """Encode a batch of texts into embeddings.

        Args:
            texts: List of text strings to encode.
            batch_size: Number of texts to process per internal batch.
                Larger values use more memory but are significantly faster.
                Default 256 balances speed and memory for typical workloads.
        """
        return self._encode(texts, batch_size=batch_size, kind="documents")

    def encode_query(self, query: str) -> list[float]:
        """Encode a single query string. Applies the query instruction prefix
        (if configured) — this must NOT be applied to indexed documents."""
        text = f"{self.query_instruction}{query}" if self.query_instruction else query
        return self._encode([text], batch_size=256, kind="query")[0]

    def _encode(self, texts: list[str], batch_size: int, kind: str) -> list[list[float]]:
        """Single choke point for all embedding work (indexing, log indexing,
        query encoding). Any failure is recorded as a structured
        ``embed_failure`` event and then re-raised unchanged, so callers'
        existing error handling is unaffected."""
        try:
            if not self.model:
                raise RuntimeError("Model not loaded. Call load() first.")
            return self.model.encode(
                texts,
                batch_size=batch_size,
                show_progress_bar=False,
                normalize_embeddings=True,
            ).tolist()
        except Exception as e:
            observability.log_embed_failure_event(
                error=e, kind=kind, batch_size=len(texts), model=self.model_name
            )
            raise
