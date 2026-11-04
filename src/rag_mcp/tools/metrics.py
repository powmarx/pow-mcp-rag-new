"""Metrics tool: get_metrics (live in-process counters)."""

import json

from rag_mcp import observability
from rag_mcp.tools import ToolContext

_ctx: ToolContext | None = None


def register(mcp, ctx: ToolContext) -> None:
    """Register metrics tools on the MCP server."""
    global _ctx
    _ctx = ctx

    mcp.tool()(get_metrics)


def get_metrics() -> str:
    """
    Return live observability counters for this server process as JSON.

    Counts are accumulated in memory since the server started and reset on
    restart: tool calls per tool (count, error calls, avg/max duration,
    zero-result calls — errors and zero-result calls never overlap),
    embedding failures (by kind and error type) and log-epoch backfills that
    updated records. For history across restarts or indexer runs, use the
    CLI: `rag-mcp get_metrics` (replays the JSONL event log).

    Returns:
        JSON document with "source": "live", "process", "tool_calls",
        "embed_failures" and "log_epoch_backfills" sections.
    """
    # Deliberately not logged as a tool_call: metrics reads would otherwise
    # inflate the very counters they report.
    return json.dumps(observability.get_metrics_snapshot(), indent=2)
