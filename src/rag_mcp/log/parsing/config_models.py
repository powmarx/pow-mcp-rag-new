"""
Log-specific configuration dataclasses for structured log indexing.

These models define the configuration schema for log patterns, line filters,
content transforms, grouping rules, and log settings. They are used by
config_loader.py when parsing log-type source entries from config.yaml.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


def _validate_name(value: str, field_name: str = "name") -> str:
    """Validate a name field: 1-64 chars, alphanumeric + underscores."""
    if not value or len(value) < 1:
        raise ValueError(f"{field_name} must be at least 1 character, got empty string")
    if len(value) > 64:
        raise ValueError(
            f"{field_name} must be at most 64 characters, got {len(value)}: '{value[:20]}...'"
        )
    if not re.match(r"^[a-zA-Z0-9_]+$", value):
        raise ValueError(
            f"{field_name} must contain only alphanumeric characters and underscores, got: '{value}'"
        )
    return value


def _validate_priority(value: int, field_name: str = "priority") -> int:
    """Validate priority: integer in range 1-999."""
    if not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer, got {type(value).__name__}")
    if value < 1 or value > 999:
        raise ValueError(f"{field_name} must be between 1 and 999, got {value}")
    return value


def _validate_action(value: str, allowed: list[str], field_name: str = "action") -> str:
    """Validate an action field against allowed values."""
    if value not in allowed:
        raise ValueError(
            f"{field_name} must be one of {allowed}, got: '{value}'"
        )
    return value


@dataclass
class LogPatternConfig:
    """A pattern for parsing log lines.

    Each pattern defines a regex with named groups that extracts structured
    fields from log lines. The 'timestamp' named group is required.
    """

    name: str
    regex: str
    event_type: str
    priority: int = 500

    def __post_init__(self) -> None:
        self.name = _validate_name(self.name, "LogPatternConfig.name")
        self.priority = _validate_priority(self.priority, "LogPatternConfig.priority")
        # Validate event_type: 1-64 chars, alphanumeric + underscores
        if not self.event_type or len(self.event_type) < 1:
            raise ValueError(
                "LogPatternConfig.event_type must be at least 1 character"
            )
        if len(self.event_type) > 64:
            raise ValueError(
                f"LogPatternConfig.event_type must be at most 64 characters, got {len(self.event_type)}"
            )
        if not re.match(r"^[a-zA-Z0-9_]+$", self.event_type):
            raise ValueError(
                f"LogPatternConfig.event_type must contain only alphanumeric characters "
                f"and underscores, got: '{self.event_type}'"
            )
        # Validate regex contains 'timestamp' named group
        if "(?P<timestamp>" not in self.regex:
            raise ValueError(
                f"LogPatternConfig.regex must contain a 'timestamp' named group "
                f"(?P<timestamp>...), pattern: '{self.name}'"
            )


@dataclass
class LineFilterConfig:
    """A filter rule for including/excluding log lines."""

    name: str
    action: str  # "include" or "exclude"
    match: str  # Python regex
    priority: int = 500

    def __post_init__(self) -> None:
        self.name = _validate_name(self.name, "LineFilterConfig.name")
        self.priority = _validate_priority(self.priority, "LineFilterConfig.priority")
        self.action = _validate_action(
            self.action, ["include", "exclude"], "LineFilterConfig.action"
        )


@dataclass
class ContentTransformConfig:
    """A content transformation rule.

    Transforms modify log line content before embedding to strip noise,
    extract specific fields, or collapse large payloads.
    """

    name: str
    match: str  # Python regex to identify target content
    action: str  # extract|replace|strip|collapse
    priority: int = 500
    fields: list[str] | None = None
    replacement: str | None = None
    max_length: int | None = None
    annotation_template: str | None = None

    def __post_init__(self) -> None:
        self.name = _validate_name(self.name, "ContentTransformConfig.name")
        self.priority = _validate_priority(
            self.priority, "ContentTransformConfig.priority"
        )
        self.action = _validate_action(
            self.action,
            ["extract", "replace", "strip", "collapse", "compact_hex"],
            "ContentTransformConfig.action",
        )


@dataclass
class GroupingRuleConfig:
    """A rule for grouping related log lines."""

    name: str
    start_pattern: str  # Regex for the first line of a group
    continuation_patterns: list[str] = field(default_factory=list)
    time_window_ms: int | None = None

    def __post_init__(self) -> None:
        if not self.name or len(self.name) < 1:
            raise ValueError(
                "GroupingRuleConfig.name must be at least 1 character"
            )
        if len(self.name) > 64:
            raise ValueError(
                f"GroupingRuleConfig.name must be at most 64 characters, got {len(self.name)}"
            )


@dataclass
class LogSettings:
    """Settings for log parsing behavior at the project level.

    These settings are shared across all log-type sources in a project.
    """

    group_time_window_ms: int = 500
    max_continuation_lines: int = 500
    max_group_lines: int = 500
    dedup_threshold: int = 3
    severity_mapping: dict[str, str] = field(default_factory=dict)
    severity_types: list[str] = field(default_factory=list)
    line_filters: list[LineFilterConfig] = field(default_factory=list)
    content_transforms: list[ContentTransformConfig] = field(default_factory=list)
    grouping_rules: list[GroupingRuleConfig] = field(default_factory=list)
    default_filter_action: str = "include"

    def __post_init__(self) -> None:
        # Validate group_time_window_ms: 10-300000
        if self.group_time_window_ms < 10 or self.group_time_window_ms > 300000:
            raise ValueError(
                f"LogSettings.group_time_window_ms must be between 10 and 300000, "
                f"got {self.group_time_window_ms}"
            )
        # Validate max_continuation_lines: 10-10000
        if self.max_continuation_lines < 10 or self.max_continuation_lines > 10000:
            raise ValueError(
                f"LogSettings.max_continuation_lines must be between 10 and 10000, "
                f"got {self.max_continuation_lines}"
            )
        # Validate max_group_lines: 10-10000
        if self.max_group_lines < 10 or self.max_group_lines > 10000:
            raise ValueError(
                f"LogSettings.max_group_lines must be between 10 and 10000, "
                f"got {self.max_group_lines}"
            )
        # Validate dedup_threshold: 2-1000
        if self.dedup_threshold < 2 or self.dedup_threshold > 1000:
            raise ValueError(
                f"LogSettings.dedup_threshold must be between 2 and 1000, "
                f"got {self.dedup_threshold}"
            )
        # Validate default_filter_action
        self.default_filter_action = _validate_action(
            self.default_filter_action,
            ["include", "exclude"],
            "LogSettings.default_filter_action",
        )
