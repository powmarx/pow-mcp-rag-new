"""
Log parser module for structured log indexing.

Parses raw log text into a flat list of LogEvent objects by applying
configured patterns. Line filtering is integrated inline: each raw line
is evaluated against filter rules BEFORE it can be attached as continuation
context or produce an event.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from rag_mcp.log.parsing.config_models import LogPatternConfig, LogSettings
from rag_mcp.log.parsing.severity import normalize_severity

if TYPE_CHECKING:
    from rag_mcp.log.parsing.line_filter import LineFilter


@dataclass
class LogEvent:
    """A single parsed log event.

    Attributes:
        timestamp: Raw timestamp string from log.
        timestamp_iso: Normalized ISO 8601 UTC string.
        severity_raw: Raw severity prefix (e.g., "ERR", "WRN").
        severity: Normalized severity: debug|info|warning|error|critical.
        message: Message body.
        device_id: Extracted device ID or "".
        command_name: Extracted command name or "".
        error_code: Extracted error code or "".
        event_type: Pattern's event_type or "unknown".
        line_number: 1-based line number in source file.
        continuation_lines: Non-matching lines attached to this event.
        pattern_name: Name of the pattern that matched.
    """

    timestamp: str
    timestamp_iso: str
    severity_raw: str
    severity: str
    message: str
    device_id: str = ""
    command_name: str = ""
    error_code: str = ""
    event_type: str = "unknown"
    line_number: int = 0
    continuation_lines: list[str] = field(default_factory=list)
    pattern_name: str = ""


# Built-in default patterns for standard log formats (used when no log_patterns configured)
_DEFAULT_PATTERNS: list[LogPatternConfig] = [
    LogPatternConfig(
        name="syslog_standard",
        regex=(
            r"(?P<timestamp>\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})"
            r"\s+\S+\s+\S+\[?\d*\]?:\s*"
            r"(?:(?P<severity>DEBUG|INFO|WARN(?:ING)?|ERROR|CRITICAL|FATAL)\s+)?"
            r"(?P<message>.*)"
        ),
        event_type="info",
        priority=900,
    ),
    LogPatternConfig(
        name="iso8601_timestamped",
        regex=(
            r"(?P<timestamp>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
            r"\s+(?:(?P<severity>DEBUG|INFO|WARN(?:ING)?|ERROR|CRITICAL|FATAL|ERR|WRN|DBG|INF)\s+)?"
            r"(?P<message>.*)"
        ),
        event_type="info",
        priority=950,
    ),
]


# Regex for extracting date from log filenames with YY-MM-DD pattern (e.g., device-26-04-28.log)
_FILENAME_DATE_PATTERN = re.compile(
    r"(\d{2})-(\d{2})-(\d{2})\.log$"
)


def extract_date_from_filename(filename: str) -> datetime | None:
    """Extract a date from log filenames with YY-MM-DD pattern.

    Supports patterns like: SomePrefix-26-04-28.log → 2026-04-28

    Args:
        filename: The log filename (basename or full path).

    Returns:
        A datetime object representing the date, or None if not matched.
    """
    match = _FILENAME_DATE_PATTERN.search(filename)
    if match:
        year_short, month, day = match.groups()
        # Assume 2000s for 2-digit years
        year = 2000 + int(year_short)
        try:
            return datetime(year, int(month), int(day), tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _normalize_timestamp(raw: str, file_date: datetime | None = None) -> str:
    """Normalize a raw timestamp to ISO 8601 UTC.

    Supported input formats (attempted in order):
    1. ISO 8601 with timezone: 2024-01-15T10:30:00.123+03:00
    2. ISO 8601 UTC: 2024-01-15T10:30:00Z
    3. Space-separated datetime: 2024-01-15 10:30:00.123
    4. Epoch seconds/milliseconds: 1705312200 or 1705312200123
    5. Relative HH:MM:SS:mmm — uses file_date parameter
    6. Fallback: current UTC time

    Args:
        raw: Raw timestamp string from log line.
        file_date: Base date for relative timestamps (from filename).

    Returns:
        ISO 8601 UTC string (e.g., "2024-01-15T10:30:00Z").
    """
    if not raw or not raw.strip():
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    stripped = raw.strip()

    # 1. Try ISO 8601 with timezone (e.g., 2024-01-15T10:30:00.123+03:00)
    try:
        dt = datetime.fromisoformat(stripped)
        if dt.tzinfo is not None:
            dt_utc = dt.astimezone(timezone.utc)
            return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            # No timezone — assume local, store with Z suffix
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        pass

    # 2. Try ISO 8601 UTC with Z suffix (fromisoformat handles Z in Python 3.11+)
    if stripped.endswith("Z"):
        try:
            dt = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError):
            pass

    # 3. Try space-separated datetime (e.g., 2024-01-15 10:30:00.123)
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(stripped, fmt)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue

    # 4. Try epoch seconds or milliseconds (pure numeric string)
    if stripped.isdigit() and len(stripped) >= 10:
        try:
            epoch_val = int(stripped)
            # If 13+ digits, treat as milliseconds
            if len(stripped) >= 13:
                epoch_sec = epoch_val / 1000.0
            else:
                epoch_sec = float(epoch_val)
            dt = datetime.fromtimestamp(epoch_sec, tz=timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, OverflowError, OSError):
            pass

    # 5. Try relative HH:MM:SS:mmm — use file_date
    relative_ts_match = re.match(r"^(\d{2}):(\d{2}):(\d{2}):(\d{3})$", stripped)
    if relative_ts_match:
        h, m, s, ms = relative_ts_match.groups()
        try:
            hour, minute, sec, millis = int(h), int(m), int(s), int(ms)
            if file_date is not None:
                dt = file_date.replace(
                    hour=hour, minute=minute, second=sec,
                    microsecond=millis * 1000, tzinfo=timezone.utc,
                )
            else:
                # No file_date — use today's date
                now = datetime.now(timezone.utc)
                dt = now.replace(
                    hour=hour, minute=minute, second=sec,
                    microsecond=millis * 1000,
                )
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, OverflowError):
            pass

    # 6. Fallback: store raw string, set timestamp_iso to current UTC time
    print(
        f"WARNING: Could not parse timestamp '{stripped}', using current UTC time",
        file=sys.stderr,
    )
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LogParser:
    """Parses raw log content into structured events using configurable patterns.

    Line filtering is applied inline during parsing: each raw line is evaluated
    against filter rules BEFORE it can be attached as continuation context or
    produce an event. This ensures excluded lines never enter the pipeline.
    """

    def __init__(
        self,
        patterns: list[LogPatternConfig] | None = None,
        settings: LogSettings | None = None,
        severity_mapping: dict[str, str] | None = None,
        line_filter: LineFilter | None = None,
        filename: str = "",
    ) -> None:
        """Initialize LogParser.

        Args:
            patterns: List of LogPatternConfig objects. If None or empty,
                built-in default patterns are used.
            settings: LogSettings instance for parsing behavior.
                If None, defaults are used.
            severity_mapping: Custom severity mapping dict. If None,
                DEFAULT_SEVERITY_MAP from severity.py is used.
            line_filter: Optional LineFilter for inline filtering.
            filename: Log filename used for date extraction (e.g.,
                device-26-04-28.log → 2026-04-28). Any filename
                matching the *-YY-MM-DD.log pattern is supported.
                The extracted date is combined with relative HH:MM:SS:mmm
                timestamps to produce valid ISO 8601 timestamps.
        """
        self._settings = settings or LogSettings()
        self._severity_mapping = severity_mapping
        self._line_filter = line_filter
        self._severity_types: set[str] = set(self._settings.severity_types)
        self._compiled_patterns: list[tuple[LogPatternConfig, re.Pattern[str]]] = []
        self._file_date: datetime | None = (
            extract_date_from_filename(filename) if filename else None
        )

        effective_patterns = patterns if patterns else _DEFAULT_PATTERNS
        self._compile_patterns(effective_patterns)

    def _compile_patterns(self, patterns: list[LogPatternConfig]) -> None:
        """Compile and sort patterns by priority (lowest first).

        Patterns with the same priority maintain their original list order.

        Args:
            patterns: List of LogPatternConfig objects to compile.
        """
        compiled: list[tuple[int, int, LogPatternConfig, re.Pattern[str]]] = []
        for idx, p in enumerate(patterns):
            regex = re.compile(p.regex)
            compiled.append((p.priority, idx, p, regex))
        # Sort by priority (lowest first), then by original index (stable)
        compiled.sort(key=lambda item: (item[0], item[1]))
        self._compiled_patterns = [(item[2], item[3]) for item in compiled]

    def _match_line(self, line: str) -> tuple[LogPatternConfig | None, re.Match[str] | None]:
        """Apply patterns in priority order. Returns first match or (None, None).

        Args:
            line: A single raw log line.

        Returns:
            Tuple of (matching config, match object) or (None, None).
        """
        for config, pattern in self._compiled_patterns:
            match = pattern.search(line)
            if match:
                return config, match
        return None, None

    def _normalize_severity(self, raw: str) -> str:
        """Map raw severity to normalized level using severity_mapping.

        If raw is empty (severity group not captured by pattern), returns 'info'.

        Args:
            raw: Raw severity string from the log line.

        Returns:
            One of: "debug", "info", "warning", "error", "critical".
        """
        return normalize_severity(raw, self._severity_mapping)

    def _is_severity_type_prefix(self, line: str) -> bool:
        """Check if a line starts with a recognized severity_type prefix.

        The prefix is the text before the first ':'. If severity_types is
        configured and the prefix is not in the set, the line is treated
        as continuation.

        Args:
            line: Raw log line text.

        Returns:
            True if the line has a recognized severity type prefix
            (or if severity_types is not configured), False otherwise.
        """
        if not self._severity_types:
            # No severity_types configured — don't use prefix check
            return True
        colon_idx = line.find(":")
        if colon_idx <= 0:
            return False
        prefix = line[:colon_idx].strip()
        return prefix in self._severity_types

    def parse(
        self,
        content: str,
        start_offset: int = 0,
        file_date: datetime | None = None,
    ) -> list[LogEvent]:
        """Parse log content into a list of LogEvent objects.

        Args:
            content: Raw log file text (from offset to EOF).
            start_offset: Line offset for line number calculation.
                The first line in content gets line_number = start_offset + 1.
            file_date: Base date for relative timestamps (e.g., from filename).
                If None, uses the date extracted from the filename passed to __init__.

        Returns:
            List of LogEvent objects, ordered by appearance.
        """
        lines = content.split("\n")
        events: list[LogEvent] = []
        current_event: LogEvent | None = None
        max_continuation = self._settings.max_continuation_lines
        effective_file_date = file_date if file_date is not None else self._file_date

        for idx, line in enumerate(lines):
            line_number = start_offset + idx + 1

            # Skip empty lines — don't attach as continuation either
            if not line.strip():
                continue

            # Apply LineFilter first — if excluded, discard entirely
            if self._line_filter is not None:
                filter_result = self._line_filter.filter_line(line)
                if not filter_result.included:
                    continue

            # Check severity_type prefix — if not recognized, treat as continuation
            if not self._is_severity_type_prefix(line):
                if current_event is not None:
                    if len(current_event.continuation_lines) < max_continuation:
                        current_event.continuation_lines.append(line)
                # If no current event, discard the line
                continue

            # Attempt pattern match
            config, match = self._match_line(line)

            if config is not None and match is not None:
                # Create a new LogEvent from the match
                groups = match.groupdict()

                raw_timestamp = groups.get("timestamp", "")
                raw_severity = groups.get("severity", "")
                message = groups.get("message", "")
                device_id = groups.get("device_id", "")
                command_name = groups.get("command_name", "")
                error_code = groups.get("error_code", "")

                timestamp_iso = _normalize_timestamp(raw_timestamp, effective_file_date)
                severity = self._normalize_severity(raw_severity)
                event_type = config.event_type if config.event_type else "unknown"

                current_event = LogEvent(
                    timestamp=raw_timestamp,
                    timestamp_iso=timestamp_iso,
                    severity_raw=raw_severity,
                    severity=severity,
                    message=message,
                    device_id=device_id,
                    command_name=command_name,
                    error_code=error_code,
                    event_type=event_type,
                    line_number=line_number,
                    continuation_lines=[],
                    pattern_name=config.name,
                )
                events.append(current_event)
            else:
                # No pattern matched — attach as continuation if possible
                if current_event is not None:
                    if len(current_event.continuation_lines) < max_continuation:
                        current_event.continuation_lines.append(line)
                # If no current event, discard the line

        return events
