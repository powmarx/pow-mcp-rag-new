"""
Event grouper module for structured log indexing.

Groups related log events into logical units and deduplicates consecutive
repetitions. The grouping algorithm applies deduplication first, then
pattern-based grouping rules with time window enforcement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from rag_mcp.log.parsing.config_models import GroupingRuleConfig, LogSettings
from rag_mcp.log.parsing.log_parser import LogEvent


# Severity hierarchy for "highest severity" aggregation
_SEVERITY_RANK: dict[str, int] = {
    "debug": 0,
    "info": 1,
    "warning": 2,
    "error": 3,
    "critical": 4,
}


def _highest_severity(a: str, b: str) -> str:
    """Return the highest severity between two normalized severity strings."""
    rank_a = _SEVERITY_RANK.get(a, 1)
    rank_b = _SEVERITY_RANK.get(b, 1)
    if rank_a >= rank_b:
        return a
    return b


def _parse_iso_timestamp(iso_str: str) -> datetime | None:
    """Parse an ISO 8601 timestamp string to a datetime object.

    Supports formats like:
    - 2024-01-15T10:30:00Z
    - 2024-01-15T10:30:00+00:00

    Returns None if parsing fails.
    """
    if not iso_str:
        return None
    try:
        # Handle Z suffix
        if iso_str.endswith("Z"):
            return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return None


def _timestamp_diff_ms(ts_start: str, ts_end: str) -> float | None:
    """Compute the difference in milliseconds between two ISO timestamps.

    Returns None if either timestamp cannot be parsed.
    """
    dt_start = _parse_iso_timestamp(ts_start)
    dt_end = _parse_iso_timestamp(ts_end)
    if dt_start is None or dt_end is None:
        return None
    delta = dt_end - dt_start
    return delta.total_seconds() * 1000.0


def _strip_timestamp_from_message(message: str) -> str:
    """Strip leading timestamp-like patterns from a message for dedup comparison.

    Removes common timestamp patterns at the start of the message:
    - ISO 8601: 2024-01-15T10:30:00Z or 2024-01-15 10:30:00.123
    - Relative: HH:MM:SS:mmm
    - Syslog: Mon DD HH:MM:SS
    """
    # Try to strip ISO 8601 timestamps at the start
    stripped = re.sub(
        r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\s*",
        "",
        message,
    )
    if stripped != message:
        return stripped

    # Try to strip relative HH:MM:SS:mmm
    stripped = re.sub(r"^\d{2}:\d{2}:\d{2}:\d{3}\s*", "", message)
    if stripped != message:
        return stripped

    # Try to strip syslog timestamps
    stripped = re.sub(r"^\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s*", "", message)
    if stripped != message:
        return stripped

    return message


@dataclass
class EventGroup:
    """A group of related log events forming a logical unit.

    Attributes:
        events: List of LogEvent objects in this group.
        line_start: First line number in the group.
        line_end: Last line number in the group.
        timestamp_start: Earliest ISO timestamp in the group.
        timestamp_end: Latest ISO timestamp in the group.
        severity: Highest severity among all events in the group.
        device_id: Common device ID or "" if mixed/absent.
        event_type: Dominant event type in the group.
        error_code: First non-empty error code found in the group.
        dedup_counts: Message text → repetition count (only for deduplicated messages).
        text: Final grouped text for embedding.
    """

    events: list[LogEvent]
    line_start: int
    line_end: int
    timestamp_start: str
    timestamp_end: str
    severity: str
    device_id: str
    event_type: str
    error_code: str
    dedup_counts: dict[str, int] = field(default_factory=dict)
    text: str = ""


class EventGrouper:
    """Groups and deduplicates log events into logical units.

    The grouping algorithm:
    1. Deduplicate consecutive identical messages (after timestamp strip)
    2. Apply grouping rules (pattern-based + time window)
    3. Produce EventGroup objects with aggregated metadata
    """

    def __init__(
        self,
        settings: LogSettings,
        grouping_rules: list[GroupingRuleConfig] | None = None,
    ) -> None:
        """Initialize EventGrouper.

        Args:
            settings: LogSettings with group_time_window_ms, max_group_lines,
                and dedup_threshold.
            grouping_rules: Optional list of GroupingRuleConfig objects defining
                pattern-based grouping. If None, only deduplication is applied
                and all events become standalone groups.
        """
        self._time_window_ms = settings.group_time_window_ms
        self._max_group_lines = settings.max_group_lines
        self._dedup_threshold = settings.dedup_threshold
        self._grouping_rules = grouping_rules or []
        self._compiled_rules: list[tuple[GroupingRuleConfig, re.Pattern[str], list[re.Pattern[str]]]] = []
        self._compile_rules()

    def _compile_rules(self) -> None:
        """Compile grouping rule patterns."""
        for rule in self._grouping_rules:
            start_pat = re.compile(rule.start_pattern)
            cont_pats = [re.compile(p) for p in rule.continuation_patterns]
            self._compiled_rules.append((rule, start_pat, cont_pats))

    def group(self, events: list[LogEvent]) -> list[EventGroup]:
        """Group events into logical units.

        Process:
        1. Deduplicate consecutive identical messages
        2. Apply grouping rules (pattern-based + time window)
        3. Produce EventGroup objects with aggregated metadata

        Args:
            events: List of LogEvent objects from the parser.

        Returns:
            List of EventGroup objects.
        """
        if not events:
            return []

        # Step 1: Deduplicate
        deduped = self._deduplicate(events)

        # Step 2: Apply grouping rules
        groups = self._apply_grouping_rules(deduped)

        # Step 3: Format text for each group
        for group in groups:
            group.text = self._format_group_text(group)

        return groups

    def _deduplicate(
        self, events: list[LogEvent]
    ) -> list[tuple[LogEvent, int]]:
        """Collapse consecutive duplicate messages into (event, count) pairs.

        A line is considered duplicate if its message text (after timestamp
        removal) is identical to the preceding line's message.
        Only collapses when count >= dedup_threshold. Otherwise, each
        event in the run is emitted individually with count=1.

        Args:
            events: List of LogEvent objects.

        Returns:
            List of (event, count) tuples. count=1 for non-deduplicated events,
            count>=dedup_threshold for deduplicated events.
        """
        if not events:
            return []

        result: list[tuple[LogEvent, int]] = []

        # Track runs of identical messages
        run_start = 0
        current_msg = _strip_timestamp_from_message(events[0].message)

        for i in range(1, len(events)):
            msg = _strip_timestamp_from_message(events[i].message)
            if msg == current_msg:
                continue
            else:
                # End of run: events[run_start..i-1] have the same message
                run_length = i - run_start
                if run_length >= self._dedup_threshold:
                    # Collapse: emit first event with count
                    result.append((events[run_start], run_length))
                else:
                    # Emit each event individually
                    for j in range(run_start, i):
                        result.append((events[j], 1))
                run_start = i
                current_msg = msg

        # Handle the last run
        run_length = len(events) - run_start
        if run_length >= self._dedup_threshold:
            result.append((events[run_start], run_length))
        else:
            for j in range(run_start, len(events)):
                result.append((events[j], 1))

        return result

    def _apply_grouping_rules(
        self, deduped: list[tuple[LogEvent, int]]
    ) -> list[EventGroup]:
        """Group events by matching rules and time window.

        For each deduped event, check if it matches a grouping rule's start
        pattern. If yes, collect subsequent events matching continuation
        patterns within the time window and up to max_group_lines.

        Events not matching any rule become standalone single-event groups.

        Args:
            deduped: List of (event, count) tuples from deduplication.

        Returns:
            List of EventGroup objects.
        """
        if not deduped:
            return []

        groups: list[EventGroup] = []
        i = 0

        while i < len(deduped):
            event, count = deduped[i]

            # Try to match a grouping rule start pattern
            matched_rule = self._match_start_pattern(event)

            if matched_rule is not None:
                rule, start_pat, cont_pats = matched_rule
                # Start a new group
                group_events: list[tuple[LogEvent, int]] = [(event, count)]
                group_start_ts = event.timestamp_iso
                time_window = rule.time_window_ms if rule.time_window_ms is not None else self._time_window_ms
                line_count = 1  # Count the start event

                # Collect continuation events
                j = i + 1
                while j < len(deduped) and line_count < self._max_group_lines:
                    next_event, next_count = deduped[j]

                    # Check time window
                    diff_ms = _timestamp_diff_ms(group_start_ts, next_event.timestamp_iso)
                    if diff_ms is not None and diff_ms > time_window:
                        break  # Time window exceeded

                    # Check if next event matches continuation patterns
                    if self._matches_continuation(next_event, cont_pats):
                        group_events.append((next_event, next_count))
                        line_count += 1
                        j += 1
                    else:
                        # Next event doesn't match continuation — stop grouping
                        break

                # Build the EventGroup
                group = self._build_event_group(group_events)
                groups.append(group)
                i = j
            else:
                # Standalone event — no rule matches
                group = self._build_event_group([(event, count)])
                groups.append(group)
                i += 1

        return groups

    def _match_start_pattern(
        self, event: LogEvent
    ) -> tuple[GroupingRuleConfig, re.Pattern[str], list[re.Pattern[str]]] | None:
        """Check if an event matches any grouping rule's start pattern.

        Matches against a reconstructed text representation that includes
        the event's pattern_name, command_name, device_id, event_type, and
        message, providing enough context for grouping rules to work even
        when the message field is sparse after pattern extraction.

        Args:
            event: The LogEvent to check.

        Returns:
            The matched rule tuple or None.
        """
        text = self._build_matchable_text(event)
        for rule, start_pat, cont_pats in self._compiled_rules:
            if start_pat.search(text):
                return (rule, start_pat, cont_pats)
        return None

    def _matches_continuation(
        self, event: LogEvent, cont_pats: list[re.Pattern[str]]
    ) -> bool:
        """Check if an event matches any of the continuation patterns.

        If no continuation patterns are defined, any event is considered
        a valid continuation (grouping is purely time-window based).

        Matches against a reconstructed text representation that includes
        the event's structured fields.

        Args:
            event: The LogEvent to check.
            cont_pats: List of compiled continuation patterns.

        Returns:
            True if the event matches a continuation pattern.
        """
        if not cont_pats:
            # No continuation patterns — any event can continue the group
            return True
        text = self._build_matchable_text(event)
        for pat in cont_pats:
            if pat.search(text):
                return True
        return False

    @staticmethod
    def _build_matchable_text(event: LogEvent) -> str:
        """Build a text representation of an event for grouping rule matching.

        Reconstructs a string containing all meaningful event fields so that
        grouping rules can match against pattern_name, command_name, event_type,
        and message content. This compensates for the fact that parsing extracts
        fields from the raw line, leaving message sparse or empty.

        The format is:
            pattern_name|command_name|event_type|message

        Args:
            event: The LogEvent to represent.

        Returns:
            A string containing all matchable event fields.
        """
        parts = [
            event.pattern_name or "",
            event.command_name or "",
            event.event_type or "",
            event.message or "",
        ]
        return "|".join(parts)

    def _build_event_group(
        self, group_events: list[tuple[LogEvent, int]]
    ) -> EventGroup:
        """Build an EventGroup from a list of (event, count) tuples.

        Aggregates metadata: highest severity, earliest/latest timestamps,
        first non-empty error_code, dominant event_type, common device_id.

        Args:
            group_events: List of (LogEvent, dedup_count) tuples.

        Returns:
            An EventGroup with aggregated metadata (text is set later).
        """
        events = [ev for ev, _ in group_events]

        # Line numbers
        line_start = events[0].line_number
        line_end = events[-1].line_number
        # Account for continuation lines of the last event
        if events[-1].continuation_lines:
            line_end = line_end + len(events[-1].continuation_lines)

        # Timestamps — earliest and latest
        timestamp_start = events[0].timestamp_iso
        timestamp_end = events[-1].timestamp_iso

        # Find actual earliest/latest by comparing all events
        for ev in events:
            if ev.timestamp_iso < timestamp_start:
                timestamp_start = ev.timestamp_iso
            if ev.timestamp_iso > timestamp_end:
                timestamp_end = ev.timestamp_iso

        # Highest severity
        severity = events[0].severity
        for ev in events[1:]:
            severity = _highest_severity(severity, ev.severity)

        # First non-empty error code
        error_code = ""
        for ev in events:
            if ev.error_code:
                error_code = ev.error_code
                break

        # Common device_id — use first non-empty, or "" if mixed
        device_id = ""
        for ev in events:
            if ev.device_id:
                device_id = ev.device_id
                break

        # Dominant event_type — use first event's type (it started the group)
        event_type = events[0].event_type

        # Build dedup_counts
        dedup_counts: dict[str, int] = {}
        for ev, count in group_events:
            if count > 1:
                dedup_counts[ev.message] = count

        return EventGroup(
            events=events,
            line_start=line_start,
            line_end=line_end,
            timestamp_start=timestamp_start,
            timestamp_end=timestamp_end,
            severity=severity,
            device_id=device_id,
            event_type=event_type,
            error_code=error_code,
            dedup_counts=dedup_counts,
            text="",  # Will be set by _format_group_text
        )

    def _format_group_text(self, group: EventGroup) -> str:
        """Format the group's events into a single text for embedding.

        Includes deduplication annotations like "[repeated 15x]" and
        continuation lines.

        Args:
            group: The EventGroup to format.

        Returns:
            Formatted text string for embedding.
        """
        lines: list[str] = []

        for event in group.events:
            msg = event.message
            # Check if this event was deduplicated
            if msg in group.dedup_counts:
                count = group.dedup_counts[msg]
                lines.append(f"{msg} [repeated {count}x]")
            else:
                lines.append(msg)

            # Add continuation lines
            for cont_line in event.continuation_lines:
                lines.append(cont_line)

        return "\n".join(lines)
