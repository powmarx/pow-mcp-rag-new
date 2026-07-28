"""Structured log indexing package for the RAG MCP Server.

This package provides log-specific parsing, filtering, transformation,
grouping, and indexing functionality for device communication logs.

The pure parsing layer lives in rag_mcp.log.parsing and has zero external
dependencies. The storage/indexing layer (LogIndexer) lives at this level.
"""

from rag_mcp.log.parsing import (
    ContentTransform,
    ContentTransformConfig,
    EventGroup,
    EventGrouper,
    FilterResult,
    GroupingRuleConfig,
    LineFilter,
    LineFilterConfig,
    LogEvent,
    LogParser,
    LogPatternConfig,
    LogPipeline,
    LogSettings,
)
from rag_mcp.log.log_indexer import LogIndexer

__all__ = [
    "ContentTransform",
    "ContentTransformConfig",
    "EventGroup",
    "EventGrouper",
    "FilterResult",
    "GroupingRuleConfig",
    "LineFilter",
    "LineFilterConfig",
    "LogEvent",
    "LogIndexer",
    "LogParser",
    "LogPatternConfig",
    "LogPipeline",
    "LogSettings",
]
