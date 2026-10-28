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
  heading, or an underlined reStructuredText / plain-text title) is never
  left orphaned at the tail of a chunk while its body starts the next one.
  An underlined title is never separated from its adornment line(s).
* Fence-aware: a fenced code block (`````` ``` `````` or ``~~~``) that fits in
  ``chunk_size`` is never split, and nothing inside a fence is ever mistaken
  for a heading. A fence larger than ``chunk_size`` must still be split
  (to honour the bound), at line boundaries.
* Breadcrumbs: every chunk carries the heading path (``"Title > Section >
  Subsection"``) of the section its own content starts in, derived from ATX
  (``#``) and underlined headings. Empty when the text has no headings
  before the chunk. The breadcrumb is metadata only; chunk content is
  untouched.
* No blank chunks: whitespace-only pieces are always folded into a neighbour.
* Balanced: text that must be split (long lines, or a section body re-flowed
  behind its heading) is divided evenly rather than greedily, so a line just
  over ``chunk_size`` becomes two halves rather than a full chunk plus a
  few-character tail.
"""

import re
from bisect import bisect_right
from dataclasses import dataclass, field

from rag_mcp.config_loader import ChunkingConfig

# A half-open [start, end) character range into the source text.
Span = tuple[int, int]

# File extensions whose chunks get a section breadcrumb stored in metadata.
# The chunker itself is content-based; this is the indexing policy for which
# document types the heading grammar is meaningful.
BREADCRUMB_EXTENSIONS = frozenset({".md", ".markdown", ".txt", ".rst"})

BREADCRUMB_SEPARATOR = " > "


def breadcrumbs_enabled(file_path: str) -> bool:
    """True if chunks of this file should carry a section breadcrumb."""
    dot = file_path.rfind(".")
    if dot == -1:
        return False
    return file_path[dot:].lower() in BREADCRUMB_EXTENSIONS


def chunk_metadatas(chunks: list["Chunk"], file_path: str) -> list[dict]:
    """Per-chunk metadata for storage: the section breadcrumb.

    The key is always present so the schema is uniform; it is empty for file
    types outside BREADCRUMB_EXTENSIONS and for chunks with no enclosing
    heading.
    """
    enabled = breadcrumbs_enabled(file_path)
    return [{"breadcrumb": c.breadcrumb if enabled else ""} for c in chunks]


@dataclass
class Chunk:
    """A single chunk of text with position metadata."""

    content: str
    index: int
    total: int
    # Heading path of the section this chunk's own content starts in,
    # e.g. "Install > Quick Start". Empty when there is no enclosing heading.
    breadcrumb: str = ""


# --------------------------------------------------------------------------- #
# Document structure (fences + headings), computed once per chunk() call
# --------------------------------------------------------------------------- #

# ATX heading: 1-6 hashes, a space, then text. Optional closing hashes.
_ATX_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
# Underline / overline adornment: a single repeated docutils punctuation char.
_ADORNMENT_RE = re.compile(r"^([=\-`:'\"~^_*+#<>])\1{2,}[ \t]*$")
# Fenced code block opener: <=3 spaces, 3+ backticks or tildes, info string.
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


@dataclass
class _Heading:
    start: int  # offset of the heading block's first line (overline if any)
    end: int  # offset just past the block's last line (incl. its newline)
    level: int  # 1-based; ATX = number of hashes, underlined = style rank
    title: str
    underlined: bool


@dataclass
class _Structure:
    """Per-document structure used to guide cutting and build breadcrumbs."""

    fences: list[Span] = field(default_factory=list)
    headings: list[_Heading] = field(default_factory=list)
    # Regions no cut may fall strictly inside: fitting fences + heading blocks.
    protected: list[Span] = field(default_factory=list)
    # Underlined heading blocks only (the ones packing treats as sections):
    # block start -> heading, and every line start in the block -> heading.
    section_at: dict[int, _Heading] = field(default_factory=dict)
    section_by_line: dict[int, _Heading] = field(default_factory=dict)
    _protected_starts: list[int] = field(default_factory=list)
    _fence_starts: list[int] = field(default_factory=list)

    def finalize(self) -> None:
        self.protected.sort()
        self._protected_starts = [s for s, _ in self.protected]
        self.fences.sort()
        self._fence_starts = [s for s, _ in self.fences]

    @staticmethod
    def _enclosing(starts: list[int], spans: list[Span], pos: int) -> Span | None:
        i = bisect_right(starts, pos) - 1
        if i >= 0 and spans[i][0] < pos < spans[i][1]:
            return spans[i]
        return None

    def protected_region(self, pos: int) -> Span | None:
        """The protected region `pos` lies strictly inside, if any."""
        return self._enclosing(self._protected_starts, self.protected, pos)

    def cut_allowed(self, pos: int) -> bool:
        return self.protected_region(pos) is None

    def in_fence(self, pos: int) -> bool:
        """True if `pos` is inside a fenced code block (any size)."""
        i = bisect_right(self._fence_starts, pos) - 1
        return i >= 0 and self.fences[i][0] <= pos < self.fences[i][1]

    def breadcrumb_at(self, pos: int) -> str:
        """Heading path enclosing `pos` (a heading starting at `pos` counts)."""
        stack: list[_Heading] = []
        for h in self.headings:
            if h.start > pos:
                break
            while stack and stack[-1].level >= h.level:
                stack.pop()
            stack.append(h)
        return BREADCRUMB_SEPARATOR.join(h.title for h in stack)


def _scan_structure(text: str, max_atomic_fence: int) -> _Structure:
    """Single line-oriented pass: find fenced code blocks and headings."""
    st = _Structure()

    # Line table: (start, end_excl_newline, next_line_start)
    lines: list[tuple[int, int, int]] = []
    pos = 0
    n = len(text)
    while pos < n:
        nl = text.find("\n", pos)
        if nl == -1:
            lines.append((pos, n, n))
            break
        lines.append((pos, nl, nl + 1))
        pos = nl + 1

    def is_blank(i: int) -> bool:
        s, e, _ = lines[i]
        return not text[s:e].strip()

    def adornment(i: int) -> str | None:
        s, e, _ = lines[i]
        m = _ADORNMENT_RE.match(text[s:e])
        return m.group(1) if m else None

    def title_candidate(i: int) -> str | None:
        s, e, _ = lines[i]
        raw = text[s:e]
        if not raw.strip() or raw[0].isspace() or adornment(i) is not None:
            return None
        return raw.rstrip()

    def adorns(i: int, char: str, title_len: int) -> bool:
        s, e, _ = lines[i]
        line = text[s:e].rstrip()
        return len(line) >= max(3, title_len) and line == char * len(line)

    style_rank: dict[tuple[str, bool], int] = {}

    def add_underlined(block_start: int, block_end: int, title: str, style: tuple[str, bool]) -> None:
        rank = style_rank.setdefault(style, len(style_rank) + 1)
        h = _Heading(block_start, block_end, rank, _clean_title(title), True)
        st.headings.append(h)
        st.protected.append((block_start, block_end))
        st.section_at[block_start] = h

    i = 0
    count = len(lines)
    while i < count:
        s, e, nxt = lines[i]
        line = text[s:e]

        # --- fenced code block ---
        m = _FENCE_OPEN_RE.match(line)
        if m and not ("`" in m.group(1) and "`" in m.group(2)):
            fence = m.group(1)
            close_re = re.compile(r"^ {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*$")
            j = i + 1
            fence_end = n
            while j < count:
                js, je, jn = lines[j]
                if close_re.match(text[js:je]):
                    fence_end = jn
                    break
                j += 1
            st.fences.append((s, fence_end))
            if fence_end - s <= max_atomic_fence:
                st.protected.append((s, fence_end))
            i = j + 1
            continue

        # --- ATX heading ---
        m = _ATX_RE.match(line)
        if m:
            st.headings.append(
                _Heading(s, nxt, len(m.group(1)), _clean_title(m.group(2)), False)
            )
            i += 1
            continue

        prev_blank = i == 0 or is_blank(i - 1)

        # --- overlined + underlined title ---
        over = adornment(i)
        if over is not None and prev_blank and i + 2 < count:
            title = title_candidate(i + 1)
            if title is not None and adorns(i, over, len(title)) and adorns(i + 2, over, len(title)):
                add_underlined(s, lines[i + 2][2], title, (over, True))
                for k in (i, i + 1, i + 2):
                    st.section_by_line[lines[k][0]] = st.section_at[s]
                i += 3
                continue

        # --- underlined title ---
        if prev_blank and i + 1 < count:
            title = title_candidate(i)
            if title is not None:
                under = adornment(i + 1)
                if under is not None and adorns(i + 1, under, len(title)):
                    add_underlined(s, lines[i + 1][2], title, (under, False))
                    for k in (i, i + 1):
                        st.section_by_line[lines[k][0]] = st.section_at[s]
                    i += 2
                    continue

        i += 1

    st.finalize()
    return st


def _clean_title(raw: str) -> str:
    return " ".join(raw.split())


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

        st = _scan_structure(text, self.chunk_size)

        # Small files: single chunk
        if len(text) <= self.chunk_size:
            return [Chunk(content=text, index=0, total=1, breadcrumb=st.breadcrumb_at(0))]

        # Split recursively using separators, working on offsets so that
        # no whitespace is ever dropped or altered.
        spans = self._recursive_split(text, 0, len(text), 0, st)

        # Pack pieces into chunks (heading-aware, re-flows long lines,
        # carries overlap from the previous chunk within chunk_size)
        packed = self._pack_spans(text, spans, st)

        # Build Chunk objects
        total = len(packed)
        return [
            Chunk(
                content=text[s:e],
                index=i,
                total=total,
                breadcrumb=st.breadcrumb_at(anchor),
            )
            for i, (s, e, anchor) in enumerate(packed)
        ]

    # ------------------------------------------------------------------ #
    # Splitting
    # ------------------------------------------------------------------ #

    def _recursive_split(
        self, text: str, start: int, end: int, separator_index: int, st: _Structure
    ) -> list[Span]:
        """Recursively split text[start:end] using separators in priority order."""
        # Base case: no more separators, force-split by chunk_size
        if separator_index >= len(self.separators):
            return self._force_split(text, start, end, st)

        separator = self.separators[separator_index]
        cut_points = self._find_cut_points(text, start, end, separator, st)

        # If separator didn't split anything useful, try next
        if not cut_points:
            return self._recursive_split(text, start, end, separator_index + 1, st)

        result: list[Span] = []
        piece_start = start
        for cut in cut_points + [end]:
            if cut <= piece_start:
                continue
            # If piece is still too large, recurse with next separator
            if cut - piece_start > self.chunk_size:
                result.extend(
                    self._recursive_split(text, piece_start, cut, separator_index + 1, st)
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
        self, text: str, start: int, end: int, separator: str, st: _Structure
    ) -> list[int]:
        """Find positions at which to cut text[start:end] on `separator`."""
        if not separator:
            return []
        offset = self._separator_cut_offset(separator)
        cuts: list[int] = []
        pos = text.find(separator, start, end)
        while pos != -1:
            cut = pos + offset
            if start < cut < end and st.cut_allowed(cut):
                cuts.append(cut)
            pos = text.find(separator, pos + len(separator), end)
        return cuts

    def _force_split(self, text: str, start: int, end: int, st: _Structure) -> list[Span]:
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
                cut = self._split_to_fit(text, lo, end, start + target - lo, st)
            if cut is None:
                # No clean cut within the unit: allow the full chunk_size
                # (giving up overlap room) before cutting mid-token.
                cut = self._split_to_fit(text, start, end, self.chunk_size, st, forced=True)
            if cut is None:
                # Pathological: a whitespace run longer than chunk_size.
                cut = start + self.chunk_size
            spans.append((start, cut))
            start = cut

        return spans

    def _split_to_fit(
        self,
        text: str,
        start: int,
        end: int,
        budget: int,
        st: _Structure,
        forced: bool = False,
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
        exceeds the budget). Cuts inside protected regions (fitting code
        fences, heading blocks) are skipped.
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
            search_end = limit
            while True:
                pos = text.rfind(separator, start, search_end)
                if pos == -1:
                    break
                cut = pos + offset
                if content_start < cut <= limit and st.cut_allowed(cut):
                    return cut
                search_end = pos

        return self._find_break_point(text, start, limit, st, forced)

    def _find_break_point(
        self, text: str, start: int, limit: int, st: _Structure, forced: bool = True
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
            if window[i].isspace() and st.cut_allowed(start + i + 1):
                return start + i + 1

        if not forced:
            return None

        # 2. No whitespace: try punctuation that usually ends a token.
        for i in range(len(window) - 1, first_content, -1):
            if window[i] in self._SOFT_BREAK_CHARS and st.cut_allowed(start + i + 1):
                return start + i + 1

        # 3. Nothing usable (e.g. base64 / minified blob): hard cut.
        return limit

    # ------------------------------------------------------------------ #
    # Section (heading) awareness
    # ------------------------------------------------------------------ #

    def _section_level(self, text: str, start: int, st: _Structure) -> int | None:
        """
        Return the section level if text[start:] begins a section, else None.

        Two sources, on one numeric scale where lower means higher in the
        hierarchy:

        * A separator with non-whitespace content (e.g. "\\n## ") marks a
          section start at its separator index (its stripped form, "## ",
          is what leads the piece after a cut). Never inside a code fence.
        * An underlined title block (reStructuredText / plain text) marks a
          section start at ``rank - 1``, where rank is the order in which its
          adornment style first appeared (the docutils convention).
        """
        if st.in_fence(start):
            return None
        heading = st.section_at.get(start)
        if heading is not None:
            return heading.level - 1
        for level, separator in enumerate(self.separators):
            marker = separator.lstrip()
            if marker and text.startswith(marker, start):
                return level
        return None

    def _ends_with_bare_heading(self, text: str, start: int, end: int, st: _Structure) -> bool:
        """True if the last non-blank line of text[start:end] is a section heading."""
        content_end = start + len(text[start:end].rstrip())
        if content_end <= start:
            return False
        line_start = max(start, text.rfind("\n", start, content_end) + 1)
        heading = st.section_by_line.get(line_start)
        if heading is not None:
            return heading.start >= start
        return self._section_level(text, line_start, st) is not None

    def _last_heading_start(self, text: str, start: int, end: int, st: _Structure) -> int | None:
        """Start offset of the last section-heading line in text[start:end]."""
        pos = end
        while pos > start:
            line_start = max(start, text.rfind("\n", start, pos - 1) + 1)
            heading = st.section_by_line.get(line_start)
            if heading is not None and heading.start >= start:
                return heading.start
            if heading is None and self._section_level(text, line_start, st) is not None:
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

    def _fold_blank_spans(self, text: str, spans: list[Span], st: _Structure) -> list[Span]:
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
                    folded.extend(self._force_split(text, prev_start, e, st))
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
        self,
        text: str,
        cur_start: int,
        cur_end: int,
        piece_end: int,
        run_end: int,
        st: _Structure,
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
        last_heading = self._last_heading_start(text, cur_start, cur_end, st)
        if last_heading is not None:
            lower = last_heading
        for lo, hi in attempts:
            while True:
                lo = max(lo, lower)
                if hi <= lo:
                    break
                cut = self._split_to_fit(text, lo, piece_end, hi - lo, st)
                if cut is None:
                    break
                if self._ends_with_bare_heading(text, cur_start, cut, st):
                    lower = cut
                    continue
                return cut
        return None

    def _overlap_start(
        self, text: str, prev_start: int, boundary: int, room: int, st: _Structure
    ) -> int:
        """
        Start offset for a new chunk whose own text begins at `boundary`.

        Reaches back up to chunk_overlap characters into the previous chunk
        (never more than `room`), then snaps forward to a line start, else a
        word start, so the chunk remains a clean substring. The previous
        chunk always keeps at least its first line or word to itself, so no
        chunk is ever entirely contained in the next one. The overlap never
        starts inside a protected region (a fitting code fence or a heading
        block); it is moved past the region instead, or dropped. Returns
        `boundary` when no overlap is possible.
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

        # Never start the overlap inside a fence or a heading block.
        region = st.protected_region(tail_start)
        if region is not None:
            tail_start = region[1]
            if tail_start >= boundary:
                return boundary

        if not text[tail_start:boundary].strip():
            return boundary
        return tail_start

    def _pack_spans(
        self, text: str, spans: list[Span], st: _Structure
    ) -> list[tuple[int, int, int]]:
        """
        Pack adjacent spans into chunks that fit within chunk_size.

        Returns ``(start, end, anchor)`` triples, where ``anchor`` is the
        offset at which the chunk's *own* text begins (after any overlap
        carried from the previous chunk); breadcrumbs are resolved there.

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

        spans = self._fold_blank_spans(text, spans, st)
        levels = [self._section_level(text, s, st) for s, _ in spans]
        small = self.chunk_size // 4

        merged: list[tuple[int, int, int]] = []
        cur_start, cur_end = spans[0]
        cur_anchor = cur_start

        def flush_and_start(boundary: int, piece_end: int) -> None:
            """Emit [cur_start, boundary); start a new chunk at `boundary`
            with as much overlap as fits alongside [boundary, piece_end)."""
            nonlocal cur_start, cur_end, cur_anchor
            merged.append((cur_start, boundary, cur_anchor))
            room = self.chunk_size - (piece_end - boundary)
            cur_start = self._overlap_start(text, cur_start, boundary, room, st)
            cur_end = piece_end
            cur_anchor = boundary

        for i in range(1, len(spans)):
            s, e = spans[i]
            is_heading = levels[i] is not None
            bare = self._ends_with_bare_heading(text, cur_start, cur_end, st)
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
                cut = self._reflow_cut(text, cur_start, cur_end, e, run_end, st)
                if cut is not None:
                    flush_and_start(cut, e)
                    continue

            flush_and_start(s, e)

        merged.append((cur_start, cur_end, cur_anchor))
        return merged
