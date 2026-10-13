"""
Text chunking with configurable separators and overlap.

Splits text into overlapping chunks using a hierarchical separator strategy,
optimized for semantic search retrieval quality.

Guarantees
----------
* Lossless: every chunk is an exact substring of the input, and with
  ``chunk_overlap=0`` the chunks partition the input exactly, so concatenating
  them reproduces the original text byte-for-byte. This matters for source
  code, where stripping indentation or collapsing blank lines changes meaning.
* Bounded: no chunk exceeds ``chunk_size``. Overlap is *included* in that
  limit: each chunk after the first begins with up to ``chunk_overlap``
  characters from the end of the previous chunk (snapped to a line or word
  boundary), shrunk when a piece leaves less room.
* Word-safe: a long line that no separator can break is cut at whitespace or
  punctuation; a hard mid-token cut happens only when no boundary exists at
  all (e.g. a base64 blob).
* Heading-aware: a piece that starts a section (e.g. a Markdown ``## ``
  heading) is never left orphaned at the tail of a chunk while its body
  starts the next one.
* No blank chunks: whitespace-only pieces are always folded into a neighbour.
* Balanced: text that must be split (long lines, or a section body re-flowed
  behind its heading) is divided evenly rather than greedily, so a line just
  over ``chunk_size`` becomes two halves rather than a full chunk plus a
  few-character tail.
"""

from dataclasses import dataclass

from rag_mcp.config_loader import ChunkingConfig

# A half-open [start, end) character range into the source text.
Span = tuple[int, int]


@dataclass
class Chunk:
    """A single chunk of text with position metadata."""

    content: str
    index: int
    total: int


class Chunker:
    """Splits text into overlapping chunks using hierarchical separators."""

    # Characters that are acceptable fallback break points when a window
    # contains no whitespace (e.g. a long argument list with no spaces).
    _SOFT_BREAK_CHARS = ",;)]}"

    def __init__(self, config: ChunkingConfig):
        self.chunk_size = config.chunk_size
        self.chunk_overlap = config.chunk_overlap
        self.separators = config.separators
        # Target size for fragments of text that must be force-split (long
        # lines, re-flowed runs). Leaves room for the overlap a chunk carries,
        # but never drops below half a chunk for degenerate overlap settings.
        self.fragment_size = max(
            self.chunk_size - self.chunk_overlap, (self.chunk_size + 1) // 2
        )

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

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

        # Split recursively using separators, working on offsets so that
        # no whitespace is ever dropped or altered.
        spans = self._recursive_split(text, 0, len(text), 0)

        # Pack pieces into chunks (heading-aware, re-flows long lines,
        # carries overlap from the previous chunk within chunk_size)
        spans = self._pack_spans(text, spans)

        # Build Chunk objects
        total = len(spans)
        return [
            Chunk(content=text[s:e], index=i, total=total)
            for i, (s, e) in enumerate(spans)
        ]

    # ------------------------------------------------------------------ #
    # Splitting
    # ------------------------------------------------------------------ #

    def _recursive_split(
        self, text: str, start: int, end: int, separator_index: int
    ) -> list[Span]:
        """Recursively split text[start:end] using separators in priority order."""
        # Base case: no more separators, force-split by chunk_size
        if separator_index >= len(self.separators):
            return self._force_split(text, start, end)

        separator = self.separators[separator_index]
        cut_points = self._find_cut_points(text, start, end, separator)

        # If separator didn't split anything useful, try next
        if not cut_points:
            return self._recursive_split(text, start, end, separator_index + 1)

        result: list[Span] = []
        piece_start = start
        for cut in cut_points + [end]:
            if cut <= piece_start:
                continue
            # If piece is still too large, recurse with next separator
            if cut - piece_start > self.chunk_size:
                result.extend(
                    self._recursive_split(text, piece_start, cut, separator_index + 1)
                )
            else:
                result.append((piece_start, cut))
            piece_start = cut

        return result

    @staticmethod
    def _separator_cut_offset(separator: str) -> int:
        """
        Offset within a separator match at which to cut.

        The leading whitespace of the separator (e.g. the "\\n" in "\\n## ")
        stays with the preceding piece, so pieces end at a line break. The
        remainder (e.g. "## ") stays with the following piece, so a heading
        is kept together with its section. Whitespace-only separators are
        kept entirely on the preceding piece.
        """
        return len(separator) - len(separator.lstrip())

    def _find_cut_points(
        self, text: str, start: int, end: int, separator: str
    ) -> list[int]:
        """Find positions at which to cut text[start:end] on `separator`."""
        if not separator:
            return []
        offset = self._separator_cut_offset(separator)
        cuts: list[int] = []
        pos = text.find(separator, start, end)
        while pos != -1:
            cut = pos + offset
            if start < cut < end:
                cuts.append(cut)
            pos = text.find(separator, pos + len(separator), end)
        return cuts

    def _force_split(self, text: str, start: int, end: int) -> list[Span]:
        """
        Split text[start:end] into fragments of at most fragment_size.

        Used when no separator could break the text (e.g. one very long line)
        and to re-split a span that grew past chunk_size. Fragments are
        balanced (a 301-char line becomes ~150 + ~151, not 300 + 1), cut at
        the best available boundary, and never whitespace-only — except when
        a whitespace run is itself longer than chunk_size. A fragment may
        exceed fragment_size (up to chunk_size) only when no clean cut exists
        within the unit.
        """
        unit = self.fragment_size
        spans: list[Span] = []
        while start < end:
            remaining = end - start
            # Remaining text fits entirely
            if remaining <= unit:
                spans.append((start, end))
                break

            cut = None
            pieces = -(-remaining // unit)  # ceil
            target = -(-remaining // pieces)
            # Don't cut so early that the rest can't fit in pieces-1 units
            lo = max(start, end - (pieces - 1) * unit)
            if start + target > lo:
                cut = self._split_to_fit(text, lo, end, start + target - lo)
            if cut is None:
                # No clean cut within the unit: allow the full chunk_size
                # (giving up overlap room) before cutting mid-token.
                cut = self._split_to_fit(text, start, end, self.chunk_size, forced=True)
            if cut is None:
                # Pathological: a whitespace run longer than chunk_size.
                cut = start + self.chunk_size
            spans.append((start, cut))
            start = cut

        return spans

    def _split_to_fit(
        self, text: str, start: int, end: int, budget: int, forced: bool = False
    ) -> int | None:
        """
        Find the best place to cut text[start:end] so the prefix is <= budget.

        Tries separators in priority order (last occurrence within budget),
        then whitespace. If `forced` (the text *must* be split), falls back to
        punctuation and finally a hard mid-token cut; otherwise returns None
        when no clean boundary exists so the caller can decline to split.

        The cut never leaves a whitespace-only piece on either side: the
        prefix always contains the range's first content character and the
        remainder always contains its last. Returns None if no acceptable cut
        exists (budget exhausted, or a leading/trailing whitespace run alone
        exceeds the budget).
        """
        if budget <= 0:
            return None

        segment = text[start:end]
        content_start = start + (len(segment) - len(segment.lstrip()))
        content_end = start + len(segment.rstrip())
        limit = min(start + budget, content_end - 1)
        if limit <= content_start:
            return None

        for separator in self.separators:
            if not separator:
                continue
            offset = self._separator_cut_offset(separator)
            pos = text.rfind(separator, start, limit)
            if pos != -1 and content_start < pos + offset <= limit:
                return pos + offset

        return self._find_break_point(text, start, limit, forced)

    def _find_break_point(
        self, text: str, start: int, limit: int, forced: bool = True
    ) -> int | None:
        """
        Return the index at which to cut text[start:limit].

        Search backward from `limit` for the last whitespace. The returned
        index is always > start so the caller makes progress, and the prefix
        always contains content (leading indentation is never split off on
        its own).

        If there is no whitespace: when `forced`, fall back to punctuation
        that usually ends a token, and finally to a hard cut at `limit`
        (e.g. a base64 blob). When not forced, return None so the caller can
        leave the token intact.
        """
        window = text[start:limit]
        first_content = len(window) - len(window.lstrip())

        # 1. Prefer the last whitespace in the window (cut *after* it so the
        #    next chunk starts on a word, and this chunk doesn't end mid-word).
        for i in range(len(window) - 1, first_content, -1):
            if window[i].isspace():
                return start + i + 1

        if not forced:
            return None

        # 2. No whitespace: try punctuation that usually ends a token.
        for i in range(len(window) - 1, first_content, -1):
            if window[i] in self._SOFT_BREAK_CHARS:
                return start + i + 1

        # 3. Nothing usable (e.g. base64 / minified blob): hard cut.
        return limit

    # ------------------------------------------------------------------ #
    # Section (heading) awareness
    # ------------------------------------------------------------------ #

    def _section_level(self, text: str, start: int) -> int | None:
        """
        Return the separator level if text[start:] begins a section, else None.

        A separator with non-whitespace content (e.g. "\\n## ") marks a section
        start; its stripped form ("## ") is what leads the piece after a cut.
        Lower level numbers are higher in the hierarchy.
        """
        for level, separator in enumerate(self.separators):
            marker = separator.lstrip()
            if marker and text.startswith(marker, start):
                return level
        return None

    def _ends_with_bare_heading(self, text: str, start: int, end: int) -> bool:
        """True if the last non-blank line of text[start:end] is a section heading."""
        content_end = start + len(text[start:end].rstrip())
        if content_end <= start:
            return False
        line_start = max(start, text.rfind("\n", start, content_end) + 1)
        return self._section_level(text, line_start) is not None

    def _last_heading_start(self, text: str, start: int, end: int) -> int | None:
        """Start offset of the last section-heading line in text[start:end]."""
        pos = end
        while pos > start:
            line_start = max(start, text.rfind("\n", start, pos - 1) + 1)
            if self._section_level(text, line_start) is not None:
                return line_start
            pos = line_start
        return None

    @staticmethod
    def _section_end(spans: list[Span], levels: list[int | None], i: int) -> int:
        """
        Return the end offset of the section that starts at spans[i].

        The section runs until the next piece that starts a section at the
        same or a higher level (a lower level number), or to the end.
        """
        level = levels[i]
        j = i + 1
        while j < len(spans) and (levels[j] is None or levels[j] > level):
            j += 1
        return spans[j - 1][1]

    # ------------------------------------------------------------------ #
    # Packing
    # ------------------------------------------------------------------ #

    def _fold_blank_spans(self, text: str, spans: list[Span]) -> list[Span]:
        """
        Absorb whitespace-only spans into a neighbour so none stands alone.

        If the absorbing span grows past chunk_size it is re-split.
        """
        folded: list[Span] = []
        for s, e in spans:
            if folded and (
                not text[s:e].strip() or not text[folded[-1][0] : folded[-1][1]].strip()
            ):
                prev_start, _ = folded.pop()
                if e - prev_start > self.chunk_size:
                    folded.extend(self._force_split(text, prev_start, e))
                else:
                    folded.append((prev_start, e))
            else:
                folded.append((s, e))
        return folded

    @staticmethod
    def _run_end(text: str, spans: list[Span], i: int) -> int:
        """
        End offset of the run of pieces starting at spans[i] that belong to
        the same line (force-split fragments end mid-line; the run ends with
        the first piece that ends in a newline).
        """
        j = i
        while j < len(spans) - 1 and text[spans[j][1] - 1] != "\n":
            j += 1
        return spans[j][1]

    def _reflow_cut(
        self, text: str, cur_start: int, cur_end: int, piece_end: int, run_end: int
    ) -> int | None:
        """
        Choose where to end the current chunk when the next piece won't fit.

        Balances [cur_start, run_end) over the minimum number of fragments so
        the split produces even pieces instead of a full chunk plus a tiny
        tail, leaving each following chunk room for its overlap. The cut lies
        in (cur_start, cur_start + chunk_size] and leaves a remainder
        [cut, piece_end) of at most fragment_size — or, as a last resort,
        fills the current chunk up to chunk_size without moving its end
        backwards. Returns None if no clean boundary exists. A cut before the
        last heading in the chunk, or one that would end the chunk on a bare
        heading, is rejected and retried further right.
        """
        cs = self.chunk_size
        unit = self.fragment_size
        total = run_end - cur_start

        attempts: list[tuple[int, int]] = []
        base = max(2, -(-total // unit))
        for pieces in (base, base + 1):
            target = -(-total // pieces)
            lo = max(piece_end - unit, run_end - (pieces - 1) * unit)
            attempts.append((lo, cur_start + target))
        # Last resort: fill the current chunk. (Clamped so it never moves the
        # chunk end backwards; in practice the chunk always ends right after
        # a boundary, so the clamp is a safety net.)
        attempts.append((max(piece_end - cs, cur_end), cur_start + cs))

        # Cuts at or before `lower` are rejected: never cut before the last
        # heading the chunk holds (it was merged to keep it with its body),
        # and never end the chunk on a bare heading.
        lower = cur_start
        last_heading = self._last_heading_start(text, cur_start, cur_end)
        if last_heading is not None:
            lower = last_heading
        for lo, hi in attempts:
            while True:
                lo = max(lo, lower)
                if hi <= lo:
                    break
                cut = self._split_to_fit(text, lo, piece_end, hi - lo)
                if cut is None:
                    break
                if self._ends_with_bare_heading(text, cur_start, cut):
                    lower = cut
                    continue
                return cut
        return None

    def _overlap_start(
        self, text: str, prev_start: int, boundary: int, room: int
    ) -> int:
        """
        Start offset for a new chunk whose own text begins at `boundary`.

        Reaches back up to chunk_overlap characters into the previous chunk
        (never more than `room`), then snaps forward to a line start, else a
        word start, so the chunk remains a clean substring. The previous
        chunk always keeps at least its first line or word to itself, so no
        chunk is ever entirely contained in the next one. Returns `boundary`
        when no overlap is possible.
        """
        reach = min(self.chunk_overlap, room)
        if reach <= 0:
            return boundary
        tail_start = max(prev_start + 1, boundary - reach)
        if tail_start >= boundary:
            return boundary

        # Unless already at a line start, snap forward to a clean break point
        # that still leaves content after it.
        if tail_start > 0 and text[tail_start - 1] != "\n":
            tail = text[tail_start:boundary]
            content_len = len(tail.rstrip())
            break_point = tail.find("\n", 0, content_len - 1)
            if break_point == -1:
                break_point = next(
                    (i for i in range(content_len - 1) if tail[i].isspace()), -1
                )
            if break_point == -1:
                return boundary
            tail_start += break_point + 1

        if not text[tail_start:boundary].strip():
            return boundary
        return tail_start

    def _pack_spans(self, text: str, spans: list[Span]) -> list[Span]:
        """
        Pack adjacent spans into chunks that fit within chunk_size.

        Each chunk after the first starts with the overlap tail of the
        previous chunk; the tail counts toward chunk_size, so the limit is
        never exceeded (the overlap shrinks instead).

        Rules that keep section headings attached to their content and avoid
        tiny fragments:

        * A piece that starts a section (e.g. a Markdown heading) is only
          merged into a chunk that already has substantial content if its
          whole section fits as well. Otherwise it begins a new chunk, so a
          heading is never orphaned at the tail of one chunk while its body
          starts the next.

        * If the current chunk is small, or is just a bare heading (possibly
          followed by sub-headings), the next piece always joins it.

        * If the next piece does not fit and the current chunk ends with a
          bare heading, ends mid-line (inside a force-split long line), or is
          small and followed by a heading, the text is re-flowed: the chunk
          boundary is moved to a clean point that balances the text across
          chunks. Otherwise structural pieces (whole lines and paragraphs)
          are never split merely to fill space.
        """
        if not spans:
            return []

        spans = self._fold_blank_spans(text, spans)
        levels = [self._section_level(text, s) for s, _ in spans]
        small = self.chunk_size // 4

        merged: list[Span] = []
        cur_start, cur_end = spans[0]

        def flush_and_start(boundary: int, piece_end: int) -> None:
            """Emit [cur_start, boundary); start a new chunk at `boundary`
            with as much overlap as fits alongside [boundary, piece_end)."""
            nonlocal cur_start, cur_end
            merged.append((cur_start, boundary))
            room = self.chunk_size - (piece_end - boundary)
            cur_start = self._overlap_start(text, cur_start, boundary, room)
            cur_end = piece_end

        for i in range(1, len(spans)):
            s, e = spans[i]
            is_heading = levels[i] is not None
            bare = self._ends_with_bare_heading(text, cur_start, cur_end)
            cur_small = cur_end - cur_start < small

            # Spans are contiguous, so merging is just extending the end.
            fits = e - cur_start <= self.chunk_size
            if fits and is_heading and not bare and not cur_small:
                section_end = self._section_end(spans, levels, i)
                fits = section_end - cur_start <= self.chunk_size

            if fits:
                cur_end = e
                continue

            mid_line = text[cur_end - 1] != "\n"
            if bare or mid_line or (is_heading and cur_small):
                run_end = self._run_end(text, spans, i)
                cut = self._reflow_cut(text, cur_start, cur_end, e, run_end)
                if cut is not None:
                    flush_and_start(cut, e)
                    continue

            flush_and_start(s, e)

        merged.append((cur_start, cur_end))
        return merged
