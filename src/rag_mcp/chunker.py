"""
Text chunking with configurable separators and overlap.

Splits text into overlapping chunks using a hierarchical separator strategy,
optimized for semantic search retrieval quality.
"""

from dataclasses import dataclass

from rag_mcp.config_loader import ChunkingConfig


@dataclass
class Chunk:
    """A single chunk of text with position metadata."""

    content: str
    index: int
    total: int


class Chunker:
    """Splits text into overlapping chunks using hierarchical separators."""

    def __init__(self, config: ChunkingConfig):
        self.chunk_size = config.chunk_size
        self.chunk_overlap = config.chunk_overlap
        self.separators = config.separators

    def chunk(self, text: str) -> list[Chunk]:
        """
        Split text into chunks.

        Files smaller than chunk_size are returned as a single chunk.
        """
        if not text or not text.strip():
            return []

        # Small files: single chunk
        if len(text) <= self.chunk_size:
            return [Chunk(content=text, index=0, total=1)]

        # Split recursively using separators
        pieces = self._recursive_split(text, 0)

        # Merge small adjacent pieces
        merged = self._merge_small_chunks(pieces)

        # Apply overlap
        if self.chunk_overlap > 0 and len(merged) > 1:
            merged = self._apply_overlap(merged)

        # Build Chunk objects
        total = len(merged)
        return [Chunk(content=c, index=i, total=total) for i, c in enumerate(merged)]

    def _recursive_split(self, text: str, separator_index: int) -> list[str]:
        """Recursively split text using separators in priority order."""
        # Base case: no more separators, force-split by chunk_size
        if separator_index >= len(self.separators):
            return self._force_split(text)

        separator = self.separators[separator_index]
        parts = text.split(separator)

        # If separator didn't split anything useful, try next
        if len(parts) <= 1:
            return self._recursive_split(text, separator_index + 1)

        result = []
        for i, part in enumerate(parts):
            # Re-add separator to beginning of non-first parts (preserves context)
            if i > 0 and separator.strip():
                part = separator + part

            part = part.strip()
            if not part:
                continue

            # If piece is still too large, recurse with next separator
            if len(part) > self.chunk_size:
                result.extend(self._recursive_split(part, separator_index + 1))
            else:
                result.append(part)

        return result

    def _force_split(self, text: str) -> list[str]:
        """Force-split text by chunk_size when no separators work."""
        chunks = []
        start = 0
        while start < len(text):
            end = start + self.chunk_size
            chunks.append(text[start:end])
            start = end
        return chunks

    def _merge_small_chunks(self, pieces: list[str]) -> list[str]:
        """Merge adjacent pieces that together fit within chunk_size."""
        if not pieces:
            return []

        merged = []
        current = pieces[0]

        for piece in pieces[1:]:
            combined_len = len(current) + len(piece) + 1  # +1 for newline
            if combined_len <= self.chunk_size:
                current = current + "\n" + piece
            else:
                merged.append(current)
                current = piece

        if current:
            merged.append(current)

        return merged

    def _apply_overlap(self, chunks: list[str]) -> list[str]:
        """Add overlap from previous chunk's tail to each chunk."""
        if len(chunks) <= 1:
            return chunks

        overlapped = [chunks[0]]
        for i in range(1, len(chunks)):
            prev_tail = chunks[i - 1][-self.chunk_overlap :]
            # Find a clean break point (newline or space)
            break_point = prev_tail.find("\n")
            if break_point == -1:
                break_point = prev_tail.find(" ")
            if break_point > 0:
                prev_tail = prev_tail[break_point + 1 :]

            if prev_tail.strip():
                overlapped.append(prev_tail + "\n" + chunks[i])
            else:
                overlapped.append(chunks[i])

        return overlapped
