"""Pure log parsing package — no storage or embedding dependencies.

This subpackage contains the complete log parsing pipeline that can be used
standalone without chromadb, embedding models, or any I/O layer:

- config_models: Dataclass configuration schema for patterns, filters, settings
- severity: Severity normalization mapping
- line_filter: Include/exclude filtering of raw log lines
- log_parser: Pattern-based parsing of raw text into LogEvent objects
- content_transform: Text transformation rules (extract, replace, strip, collapse)
- event_grouper: Grouping related events into logical units
- log_pipeline: Orchestrator that chains all stages together

Usage:
    from rag_mcp.log.parsing import LogPipeline, LogParser, LogSettings

    pipeline = LogPipeline(
        LogParser(),
        ContentTransform([]),
        EventGrouper(LogSettings()),
    )
    groups = pipeline.process(raw_log_content)
"""

from rag_mcp.log.parsing.config_models import (
    ContentTransformConfig,
    GroupingRuleConfig,
    LineFilterConfig,
    LogPatternConfig,
    LogSettings,
)
from rag_mcp.log.parsing.content_transform import ContentTransform
from rag_mcp.log.parsing.event_grouper import EventGroup, EventGrouper
from rag_mcp.log.parsing.line_filter import FilterResult, LineFilter
from rag_mcp.log.parsing.log_parser import LogEvent, LogParser
from rag_mcp.log.parsing.log_pipeline import LogPipeline
from rag_mcp.log.parsing.severity import DEFAULT_SEVERITY_MAP, normalize_severity

__all__ = [
    "ContentTransform",
    "ContentTransformConfig",
    "DEFAULT_SEVERITY_MAP",
    "EventGroup",
    "EventGrouper",
    "FilterResult",
    "GroupingRuleConfig",
    "LineFilter",
    "LineFilterConfig",
    "LogEvent",
    "LogParser",
    "LogPatternConfig",
    "LogPipeline",
    "LogSettings",
    "normalize_severity",
]
