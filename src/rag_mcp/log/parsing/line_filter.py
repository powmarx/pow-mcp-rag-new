"""
Line filtering module for structured log indexing.

Evaluates include/exclude rules against individual log lines. Used inline by
LogParser during parsing — not as a separate pipeline stage. This ensures
excluded lines are never attached as continuation context or produce events.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rag_mcp.log.parsing.config_models import LineFilterConfig

if TYPE_CHECKING:
    pass


@dataclass
class FilterResult:
    """Result of applying filters to a line or event.

    Attributes:
        included: Whether the line/event should be included in the pipeline.
        captured_groups: Named groups captured from the matching filter's regex.
    """

    included: bool
    captured_groups: dict[str, str] = field(default_factory=dict)


class LineFilter:
    """Evaluates include/exclude rules against log lines.

    Filters are evaluated in priority order (lowest priority value first).
    The first matching filter determines whether a line is included or excluded.
    If no filter matches, the configured default action is applied.
    """

    def __init__(
        self, filters: list[LineFilterConfig], default_action: str = "include"
    ) -> None:
        """Initialize LineFilter with filter configurations.

        Args:
            filters: List of LineFilterConfig objects defining filter rules.
            default_action: Action to apply when no filter matches.
                Must be "include" or "exclude". Defaults to "include".
        """
        if default_action not in ("include", "exclude"):
            raise ValueError(
                f"default_action must be 'include' or 'exclude', got: '{default_action}'"
            )
        self._default_action = default_action
        self._compiled_filters: list[tuple[LineFilterConfig, re.Pattern[str]]] = []
        self._compile_filters(filters)

    def _compile_filters(self, filters: list[LineFilterConfig]) -> None:
        """Compile filter regex patterns and sort by priority (lowest first).

        Args:
            filters: List of LineFilterConfig objects to compile.
        """
        compiled: list[tuple[LineFilterConfig, re.Pattern[str]]] = []
        for f in filters:
            pattern = re.compile(f.match)
            compiled.append((f, pattern))
        # Sort by priority (lowest value = highest priority, evaluated first)
        compiled.sort(key=lambda item: item[0].priority)
        self._compiled_filters = compiled

    def filter_line(self, line: str) -> FilterResult:
        """Evaluate a single raw line against filters.

        Called by LogParser for every line BEFORE pattern matching or
        continuation attachment. If excluded, the line is discarded entirely.

        Args:
            line: The raw log line text to evaluate.

        Returns:
            FilterResult with inclusion decision and any captured named groups.
        """
        for config, pattern in self._compiled_filters:
            match = pattern.search(line)
            if match:
                included = config.action == "include"
                captured_groups = {
                    k: v for k, v in match.groupdict().items() if v is not None
                }
                return FilterResult(included=included, captured_groups=captured_groups)

        # No filter matched — apply default action
        included = self._default_action == "include"
        return FilterResult(included=included, captured_groups={})

    def filter_event(self, event: object) -> FilterResult:
        """Determine if a fully parsed event should be included or excluded.

        Called after event construction for a secondary check (e.g.,
        severity-based filtering). Evaluates filters against the event's
        message field.

        Args:
            event: A LogEvent object (or any object with a 'message' attribute).

        Returns:
            FilterResult with inclusion decision and any captured named groups.
        """
        # Extract message from event — supports any object with a message attribute
        message = getattr(event, "message", "")
        return self.filter_line(message)
