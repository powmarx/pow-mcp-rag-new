# Log Indexing Guide

This guide explains how to set up and use structured log indexing with the RAG MCP Server.

## Overview

The structured log indexing feature parses device communication logs into searchable events with rich metadata — severity, timestamps, error codes, device IDs, and event types. Instead of indexing raw log text as opaque chunks, the system extracts structured fields that enable precise filtering via the `search_logs` MCP tool.

The architecture is **data-driven**: all parsing behavior is defined in YAML configuration files. Supporting a new log format requires only writing a new config — no code changes needed.

## Quick Start

### 1. Create a log pattern configuration

Create a YAML config for your log format. See [LOG_PATTERN_CONFIGURATION.md](LOG_PATTERN_CONFIGURATION.md) for the full schema reference.

Minimal example:

```yaml
- name: my_project_logs
  base_path: "${PROJECTS_ROOT}/my-project/logs"
  sources:
    - pattern: "*.log"
      type: log
      description: "My project logs"
      log_patterns:
        - name: my_general
          regex: '(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(?P<severity>\w+)\s+(?P<message>.*)'
          event_type: info
          priority: 100
  log_settings:
    severity_mapping:
      ERROR: error
      WARN: warning
      INFO: info
      DEBUG: debug
```

### 2. Add the project to config.yaml

Add your project entry to `config/config.yaml` under the `projects:` list.

### 3. Index your logs

Use the `index_log_file` MCP tool from Kiro (recommended for large files):

- "Index the log file my_project.log"
- "Index my_project.log between 14:00 and 15:00"

Or from the command line for initial bulk indexing of small files:
```bash
.venv\Scripts\python.exe indexer.py --project my_project_logs
```

**Note:** The background reindex only processes log files under 5 MB. For larger files, use the `index_log_file` tool with an optional time window.

### 4. Search with search_logs

From Kiro or any MCP client:
- "Search logs for errors in my_project_logs"
- "Find log events with error code 9F00 in the last hour"
- "Show me all ERROR severity events from today"

---

## On-Demand Indexing (index_log_file)

For large log files (>5 MB), the background reindex skips them automatically. Use the `index_log_file` MCP tool instead:

### Basic usage

```
"Index the log file my_device-2026-04-28.log"
"Index my_device-2026-06-03.log"
```

### With time window (recommended for large files)

```
"Index my_device-2026-04-28.log between 14:00 and 15:00"
"Index my_device-2026-05-14.log from 10:00:00 to 11:30:00"
```

A 576 MB file with a 1-hour time window takes **seconds**. Without a time window, it takes ~15-20 minutes.

### How it works

1. Reads the file in 10k-line streaming batches (never loads entire file into memory)
2. Each batch goes through: parse → line filter → time filter → transform → group → embed → store
3. Progress reported as percentage every ~100k lines
4. Chunks use deterministic IDs (based on line numbers) — re-running is idempotent

### Cancellation

If indexing takes too long:

```
"Cancel the current indexing"
```

The `cancel_indexing` tool stops processing after the current batch (~2 seconds). Already-stored chunks are preserved. Re-running `index_log_file` on the same file continues seamlessly.

**Important:** Always use `cancel_indexing` instead of killing the process. Force-killing leaves PyTorch DLLs locked, requiring a reboot.

### Project configuration

Set `auto_reindex: false` in `config.yaml` for log-heavy projects to prevent the background reindex from attempting them:

```yaml
- name: my_device_logs
  auto_reindex: false
  base_path: C:\path\to\logs
  sources:
    - pattern: "**/*.log"
      type: log
```

---

## Configuration Reference

### Source type: log

In `config.yaml`, a source with `type: log` routes files through the log-specific pipeline instead of the standard text chunker.

```yaml
sources:
  - pattern: "*.log"
    type: log
    description: "Device communication logs"
    log_patterns:
      - name: pattern_name
        regex: '...'
        event_type: info
        priority: 100
```

### log_settings (project-level)

Shared settings across all log-type sources in a project:

| Field | Type | Default | Range | Description |
|-------|------|---------|-------|-------------|
| `group_time_window_ms` | int | 500 | 10–300000 | Time window for grouping related events |
| `max_continuation_lines` | int | 500 | 10–10000 | Max non-matching lines attached to an event |
| `max_group_lines` | int | 500 | 10–10000 | Max lines in a single group |
| `dedup_threshold` | int | 3 | 2–1000 | Min consecutive duplicates to collapse |
| `default_filter_action` | str | "include" | include/exclude | Action when no filter matches |
| `severity_mapping` | dict | {} | — | Raw prefix → normalized level mapping |
| `severity_types` | list | [] | — | Recognized raw severity prefixes |
| `line_filters` | list | [] | — | Include/exclude filter rules |
| `content_transforms` | list | [] | — | Content transformation rules |
| `grouping_rules` | list | [] | — | Event grouping rules |

**Tip:** For noisy device logs (80%+ debug lines), use `default_filter_action: exclude` with explicit include rules for the severities you care about. This can reduce indexed events by 95%+.

### Project-level settings

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `auto_reindex` | bool | true | If false, skip this project during background reindex |

### log_patterns

Each pattern defines how to parse a log line type. See [LOG_PATTERN_CONFIGURATION.md](LOG_PATTERN_CONFIGURATION.md) for detailed guidance.

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique identifier (1–64 chars, alphanumeric + underscores) |
| `regex` | Yes | Python regex with `(?P<timestamp>...)` required group |
| `event_type` | Yes | Classification: command, response, error, warning, info, etc. |
| `priority` | No | 1–999 (default 500). Lower = matched first. |

### line_filters

Rules to include/exclude lines before parsing:

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique identifier |
| `action` | Yes | "include" or "exclude" |
| `match` | Yes | Python regex tested against the raw line |
| `priority` | No | 1–999 (default 500). Lower = evaluated first. |

### content_transforms

Rules to modify event text before embedding:

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique identifier |
| `match` | Yes | Python regex to find target content |
| `action` | Yes | extract, replace, strip, or collapse |
| `priority` | No | 1–999 (default 500). Lower = applied first. |
| `fields` | extract only | Named groups to retain |
| `replacement` | replace only | Substitution string (supports backreferences) |
| `max_length` | collapse only | Max characters before truncation |
| `annotation_template` | collapse only | Template appended after truncation (e.g., `"... [{byte_count} bytes]"`) |

### grouping_rules

Rules for combining related events into logical units:

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique identifier |
| `start_pattern` | Yes | Regex matching the first line of a group |
| `continuation_patterns` | No | List of regexes for subsequent lines |
| `time_window_ms` | No | Override group_time_window_ms for this rule |

---

## search_logs Tool Reference

The `search_logs` MCP tool searches indexed log events with structured filtering and semantic search.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `query` | string | No* | Semantic search on log message content (max 512 chars) |
| `project` | string | No | Filter by project name |
| `severity` | string | No | One of: DEBUG, INFO, WARNING, ERROR, CRITICAL |
| `time_range_start` | string | No | ISO 8601 start time (e.g., "2026-04-28T05:00:00Z") |
| `time_range_end` | string | No | ISO 8601 end time |
| `error_code_pattern` | string | No | Prefix match on error codes (max 32 chars) |
| `device_id` | string | No | Filter by device identifier |
| `event_type` | string | No | Filter by event type (exact match) |
| `top_k` | integer | No | Max results, 1–50 (default 20) |

*At least one of `query` or a filter parameter must be provided.

### Behavior

- **With query**: Encodes the query into a vector, finds semantically similar chunks, applies filters as ChromaDB `where` clause.
- **Without query (filters only)**: Returns matching chunks ordered by timestamp (newest first).
- **error_code_pattern**: Uses prefix matching (e.g., "88a1" matches "88a15300", "88a15310").
- **Invalid severity**: Returns an error message listing accepted values.
- **No criteria**: Returns an error message requesting at least one search criterion.
- **No results**: Returns an empty list with count zero.

### Examples

```
# Semantic search for dispense failures
search_logs(query="dispense failed", project="my_device_logs")

# Filter by severity and time range
search_logs(severity="ERROR", time_range_start="2026-04-28T05:00:00Z", time_range_end="2026-04-28T06:00:00Z")

# Error code prefix matching
search_logs(error_code_pattern="88a153", project="my_device_logs")

# Combine semantic search with filters
search_logs(query="serial exchange", severity="ERROR", event_type="error")

# Filter-only (results ordered by timestamp descending)
search_logs(event_type="command", device_id="my_device.dll")
```

---

## Incremental Indexing

Log files are indexed incrementally using byte-offset tracking:

1. **First run**: Indexes the entire file from byte 0.
2. **Subsequent runs**: Reads only from the stored offset to EOF, finds the first complete entry boundary, then parses and indexes the new content.
3. **File truncation/rotation**: If the file size is smaller than the stored offset, the offset resets to 0 and the file is re-indexed entirely.
4. **Error handling**: If parsing fails, the previous offset is retained (no data loss). If the offset update fails after successful indexing, chunks are kept (no rollback).

Offsets are stored as special metadata records in ChromaDB with `record_type: "offset_tracker"`. These records use zero-vector embeddings so they never appear in search results.

---

## Pipeline Architecture

The log indexing pipeline processes files through these stages:

```
Raw log file
    │
    ▼
┌─────────────────────┐
│   Line Filter       │  ← Include/exclude rules (before parsing)
└─────────────────────┘
    │
    ▼
┌─────────────────────┐
│   Log Parser        │  ← Pattern matching + field extraction
└─────────────────────┘
    │
    ▼
┌─────────────────────┐
│  Content Transform  │  ← Clean/extract/strip/collapse text
└─────────────────────┘
    │
    ▼
┌─────────────────────┐
│   Event Grouper     │  ← Deduplication + rule-based grouping
└─────────────────────┘
    │
    ▼
┌─────────────────────┐
│   Embedding + Store │  ← Generate vectors, store in ChromaDB
└─────────────────────┘
```

Each stage is independent and configurable. The parsing layer (`log_parser`, `line_filter`, `content_transform`, `event_grouper`, `severity`) has no dependencies on the indexing/storage layer — it can be used standalone.

---

## Troubleshooting

### No events parsed from a log file

- Check that `severity_types` includes all prefixes used in your log format. Lines whose first token (before `:`) is NOT in `severity_types` are treated as continuation lines.
- Verify your regex patterns compile: `python -c "import re; re.compile(r'YOUR_REGEX')"`
- Ensure the `timestamp` named group is present in each pattern.
- Check that line filters aren't excluding the lines you expect to parse.

### Events are too fragmented (many small chunks)

- Add grouping rules to combine related lines (e.g., command begin → end pairs).
- Increase `group_time_window_ms` to group events over a wider time span.
- Increase `max_group_lines` if groups are being split prematurely.

### Too many duplicate events

- Lower `dedup_threshold` to deduplicate smaller runs of identical lines.
- Verify that deduplication compares messages after timestamp removal — timestamps don't affect identity.

### search_logs returns no results

- Verify the project was indexed: check `indexer.py` output for chunk counts.
- If using time range filters, ensure timestamps are in ISO 8601 UTC format.
- If using severity filter, ensure it's uppercase (DEBUG, INFO, WARNING, ERROR, CRITICAL).
- Try a broader query or fewer filters to confirm data exists.

### File appears to re-index completely each time

- This happens when the file is truncated or rotated between runs (offset resets to 0).
- For rotated logs, consider indexing a directory pattern that picks up all rotated files.

---

## Reference Implementation

See [LOG_PATTERN_CONFIGURATION.md](LOG_PATTERN_CONFIGURATION.md) for a complete configuration
walkthrough — patterns covering commands, responses, errors, warnings, state changes, and
diagnostics; line filters excluding hex dumps and debug noise; content transforms extracting
protocol data and collapsing large payloads; and grouping rules for command lifecycles and
serial exchanges.
