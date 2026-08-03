"""
Log parsing pipeline module.

Orchestrates the pure parsing stages: boundary detection → parser selection →
parse → transform → group. Returns structured EventGroup objects with no
dependency on storage or embedding infrastructure.

This module is completely isolated from ChromaDB, embedding models, and any
I/O layer, making it usable standalone for log analysis, testing, or
integration into different backends.
"""

from __future__ import annotations

import re
from datetime import datetime

from rag_mcp.log.parsing.config_models import LogPatternConfig, LogSettings
from rag_mcp.log.parsing.content_transform import ContentTransform
from rag_mcp.log.parsing.event_grouper import EventGroup, EventGrouper
from rag_mcp.log.parsing.line_filter import LineFilter
from rag_mcp.log.parsing.log_parser import LogEvent, LogParser


class LogPipeline:
    """Pure parsing pipeline: parse → transform → group.

    Encapsulates the log processing stages that are independent of storage:
    1. Find the first complete log entry boundary in content
    2. Detect whether WDATA/RDATA exists; if not, enable DMP fallback filters
    3. Parse content into LogEvent objects via LogParser (inline LineFilter)
    4. Apply ContentTransform to each event's text (message + continuation)
    5. Group events via EventGrouper into logical units

    All dependencies are standard-library or intra-package. No chromadb,
    no embedding models, no I/O.
    """

    def __init__(
        self,
        log_parser: LogParser,
        content_transform: ContentTransform,
        event_grouper: EventGrouper,
        settings: LogSettings | None = None,
        patterns: list[LogPatternConfig] | None = None,
    ) -> None:
        """Initialize LogPipeline with parsing components.

        Args:
            log_parser: Parser for extracting structured events from raw log text.
            content_transform: Transformer for cleaning event text before grouping.
            event_grouper: Grouper for combining related events into logical units.
            settings: Optional LogSettings for DMP fallback filter access.
            patterns: Optional list of log patterns for rebuilding parser with fallback.
        """
        self._parser = log_parser
        self._transform = content_transform
        self._grouper = event_grouper
        self._settings = settings
        self._patterns = patterns

    @property
    def parser(self) -> LogParser:
        """Access the underlying LogParser instance."""
        return self._parser

    def find_entry_boundary(self, content: str) -> int:
        """Find the character offset of the first complete log entry in content.

        Scans forward from the start of content until a line matches one of
        the configured patterns, ensuring we don't start mid-entry after an
        offset seek.

        Args:
            content: The raw log content to scan.

        Returns:
            Character offset of the first line that matches a configured pattern.
            Returns 0 if no match is found (process everything) or content is empty.
        """
        if not content:
            return 0

        lines = content.split("\n")
        char_offset = 0

        for line in lines:
            stripped = line.strip()
            if not stripped:
                char_offset += len(line) + 1  # +1 for the newline
                continue

            config, match = self._parser._match_line(line)
            if config is not None and match is not None:
                return char_offset

            char_offset += len(line) + 1  # +1 for the newline

        # No pattern matched any line — return 0 to process everything
        return 0

    def select_parser(self, content: str) -> LogParser:
        """Select the appropriate parser based on content characteristics.

        If the content matches the dmp_fallback_detect pattern (configured in
        log_settings), the standard parser is used. If no match is found
        (old-format logs), a fallback parser is created that also includes.

        Args:
            content: The log content to analyze.

        Returns:
            LogParser instance — either the standard one or a fallback with
            DMP filters enabled.
        """
        if (
            not self._settings
            or not self._settings.dmp_fallback_detect
            or not self._settings.dmp_fallback_filters
            or not self._patterns
        ):
            return self._parser

        detect_pattern = re.compile(self._settings.dmp_fallback_detect)
        if detect_pattern.search(content):
            return self._parser

        # Detection pattern not found — build parser with DMP fallback filters
        combined_filters = list(self._settings.line_filters) + list(
            self._settings.dmp_fallback_filters
        )
        fallback_line_filter = LineFilter(
            filters=combined_filters,
            default_action=self._settings.default_filter_action,
        )

        return LogParser(
            patterns=self._patterns,
            settings=self._settings,
            severity_mapping=self._settings.severity_mapping or None,
            line_filter=fallback_line_filter,
            filename=self._parser._file_date.strftime("%y-%m-%d.log")
            if self._parser._file_date
            else "",
        )

    def transform_events(self, events: list[LogEvent]) -> list[LogEvent]:
        """Apply ContentTransform to each event's full text.

        Combines each event's message with its continuation lines, applies
        the configured transforms, and updates the event in-place.

        Args:
            events: List of LogEvent objects to transform.

        Returns:
            The same list of events (modified in-place) with transformed text.
        """
        for event in events:
            full_text = event.message
            if event.continuation_lines:
                full_text += "\n" + "\n".join(event.continuation_lines)

            transformed = self._transform.transform(full_text)

            event.message = transformed
            event.continuation_lines = []

        return events

    def process(
        self,
        content: str,
        start_offset: int = 0,
        file_date: datetime | None = None,
    ) -> list[EventGroup]:
        """Run the full parsing pipeline on raw log content.

        Executes all pure-parsing stages in sequence:
        1. Find entry boundary (skip partial entries)
        2. Select parser (standard or DMP fallback)
        3. Parse into LogEvent objects
        4. Apply content transforms
        5. Group into EventGroups

        Args:
            content: Raw log file text.
            start_offset: Line offset for line number calculation.
            file_date: Base date for relative timestamps.

        Returns:
            List of EventGroup objects ready for embedding/storage.
            Returns empty list if content is empty or produces no events.
        """
        if not content or not content.strip():
            return []

        # Step 1: Find entry boundary
        boundary_offset = self.find_entry_boundary(content)
        effective_content = content[boundary_offset:]

        if not effective_content.strip():
            return []

        # Step 2: Select parser
        parser = self.select_parser(effective_content)

        # Step 3: Parse content into LogEvent objects
        events = parser.parse(effective_content, start_offset=start_offset, file_date=file_date)

        if not events:
            return []

        # Step 4: Apply content transforms
        self.transform_events(events)

        # Step 5: Group events
        groups = self._grouper.group(events)

        return groups
