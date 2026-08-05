# Log Pattern Configuration Guide

This document explains how to create and maintain log pattern configuration files for the structured log indexing pipeline. The YAML config file drives all parsing behavior — no code changes needed to support new log formats.

## Overview

The log indexing pipeline is **data-driven**: a YAML configuration file defines how raw log lines are parsed into structured events. The generic engine reads the config and applies it to any log format. Each project can have its own log pattern configuration.

## Configuration Structure

```yaml
project_config:
  name: my_project_logs
  base_path: /path/to/logs
  sources:
    - pattern: "*.log"
      type: log
      log_patterns: [...]       # How to recognize and extract fields from lines
  log_settings:
    severity_types: [...]       # Recognized severity prefixes
    severity_mapping: {...}     # Raw prefix → normalized level
    line_filters: [...]         # Include/exclude rules
    content_transforms: [...]   # Clean content before embedding
    grouping_rules: [...]       # Combine related lines into events
```

---

## Log Patterns

A log pattern is a rule that tells the parser how to recognize and extract structured data from a raw log line. Each pattern is a Python regex with named capture groups. When a line matches a pattern, the captured groups become metadata fields on the indexed event (searchable via `search_logs`).

Lines that don't match ANY pattern become "continuation lines" — they get attached to the preceding matched event as context (up to `max_continuation_lines`).

### Pattern Fields

Each pattern entry has 4 fields:

#### `name`

A unique identifier (1–64 chars, alphanumeric + underscores).

**Convention**: `<system>_<what_it_matches>`

Examples:
- `cache_buffer` — matches cache buffer
- `system_api_call` — matches command execution API calls
- `system_api_warning` — matches API warnings

Tips:
- Use a prefix that identifies the system (e.g., `system`)
- Use a suffix that describes the content semantics (e.g., `_error`, `_call`)
- Keep it short but unambiguous within the project

#### `event_type`

Classifies what this log line represents semantically. This is a **free-form string** (1–64 chars). The `search_logs` tool accepts any value as a filter — no validation against a fixed list.

**Recommended values** (but custom values are fully supported):

| Value | Use when... | Examples |
|---|---|---|
| `command` | Sending a request or initiating an operation | API calls, system calls |
| `response` | Receiving a result or completion status | execution commands (with return code), buffers |
| `error` | A failure or abnormal condition | error lines, hex error codes, command error |
| `warning` | Non-fatal issue that deserves attention | warning lines, API warnings |
| `info` | Normal operational information | information messages |
| `state_change` | System transitioning between states | begin/end markers, status transitions |
| `diagnostic` | Low-level trace/debug data |debug markers, hex dumps |

**Custom examples**: `heartbeat`, `metric`, `crypto_handshake`

**Decision flowchart**:
1. Is this line INITIATING an action? → `command`
2. Is this line REPORTING a result? → `response`
3. Is this line REPORTING a failure? → `error`
4. Is this line REPORTING a concern? → `warning`
5. Is this line marking a TRANSITION? → `state_change`
6. Is this line raw TRACE data? → `diagnostic`
7. Otherwise → `info` (or define your own custom type)

#### `priority`

Determines which pattern matches first when multiple patterns could match the same line. **Lower number = higher priority** (matched first). Range: 1–999 (default 500 if omitted).

**Guidelines for assigning priority**:

| Range | Use for |
|---|---|
| 1–19 | Very specific, high-value lines (protocol data with many structured fields) |
| 20–49 | Important operational markers (command begin/end, serial dispatch) |
| 50–69 | Error/warning lines with specific structured captures |
| 70–89 | State change markers (INI/FIM, DBX) |
| 90–99 | Low-level diagnostic lines |
| 100 | General catch-all (should be exactly ONE per config) |

**Key rules**:
- More specific patterns get LOWER numbers (higher priority)
- The general catch-all must have the HIGHEST number
- If two patterns could match the same line, the one extracting MORE useful fields should have lower priority (so it wins)


#### `regex`

Python-compatible regex with named capture groups.

**Required group**: `(?P<timestamp>...)` — the time portion of the line.

**Optional groups** (become searchable metadata):
- `(?P<severity>...)` — raw severity prefix (will be normalized via `severity_mapping`)
- `(?P<system_id>...)` — identifies which DLL/module/system produced the line
- `(?P<command_name>...)` — the command or operation being performed
- `(?P<error_code>...)` — error/status code for filtering
- `(?P<message>...)` — the main content for semantic search

**Additional groups** (e.g., `(?P<wdata>...)`, `(?P<class>...)`) are captured but stored as part of the event text, not as separate metadata fields.

### How to Add a New Pattern

1. **Identify** a log line type you want to extract structured data from:
   ```
   ERR:05:01:02:004|System.dll|SystemClass|API_CALL: error = 999999
   ```

2. **Write a regex** with named groups to capture the fields you need:
   ```
   (?P<severity>ERR):(?P<timestamp>\d{2}:\d{2}:\d{2}:\d{3})\|(?P<device_id>[^|]+)\|ThrId:\s+\d+\s+(?:\|[^|]+\|)?API_CALL:\s+last_error\s+=\s+(?P<error>[0-9A-Fa-f]{8})(?P<message>.*)
   ```

3. **Choose an event_type**: `error` (this is reporting a failure)

4. **Assign a priority**: 35 (specific error pattern, should win over generic ERR catch-all at 50)

5. **Add to the `log_patterns` list**:
   ```yaml
   - name: system_api_err
     regex: '(?P<severity>ERROR):(?P<timestamp>\d{2}:\d{2}:\d{2}:\d{3})\|...'
     event_type: error
     priority: 35
   ```

6. **Test**: `python -c "import re; c = re.compile(r'YOUR_REGEX'); assert 'timestamp' in c.groupindex"`

---

## Line Filters

Rules that include/exclude lines BEFORE they enter the parsing pipeline. Priority order (lowest first); first match wins.

```yaml
line_filters:
  - name: exclude_dmp_hex       # Unique identifier
    action: exclude             # "include" or "exclude"
    match: '^DMP:\s+[0-9A-Fa-f]{2}\s+'  # Python regex
    priority: 100               # Lower = evaluated first
```

- Lines matching an `exclude` filter are discarded entirely (never attached as continuation, never grouped, never embedded)
- Lines matching an `include` filter are always kept (useful to protect specific lines when `default_filter_action` is `exclude`)
- Lines matching NO filter use `default_filter_action` (default: `include`)

---

## Content Transforms

Rules that modify event text after filtering but before embedding. Used to strip noise and extract meaningful data.

```yaml
content_transforms:
  - name: my_transform
    match: 'REGEX_TO_FIND_TARGET'    # What to look for
    action: extract|replace|strip|collapse
    priority: 100
```

**Actions**:

| Action | What it does | Extra fields |
|---|---|---|
| `extract` | Keep only named capture group values, discard rest | `fields: ["group1", "group2"]` |
| `replace` | Substitute matched text with replacement | `replacement: "new text"` (supports `\1`, `\g<name>`) |
| `strip` | Remove matched portion entirely | — |
| `collapse` | Truncate to max_length with annotation | `max_length: 64`, `annotation_template: "... [{byte_count} bytes]"` |

---

## Grouping Rules

Rules for combining multiple related log lines into a single logical event before embedding.

```yaml
grouping_rules:
  - name: my_group
    start_pattern: 'REGEX_FOR_FIRST_LINE'
    continuation_patterns:
      - 'REGEX_FOR_SUBSEQUENT_LINES'
      - 'ANOTHER_CONTINUATION_PATTERN'
    time_window_ms: 5000   # Max time span for the group
```

- The group starts when a line matches `start_pattern`
- Subsequent lines matching any `continuation_patterns` are included
- The group closes when: time window is exceeded, `max_group_lines` is reached, or a new `start_pattern` matches

---

## Severity Settings

### `severity_types`

List of recognized raw severity prefixes. Lines whose first token (before `:`) is NOT in this list are treated as continuation lines (attached to the preceding event).

```yaml
severity_types: ["INI", "FIM", "WRN", "ERR", "STA", "INF", "DBG", "DBX", "DMP"]
```

### `severity_mapping`

Maps raw prefix to normalized levels (`critical`, `error`, `warning`, `info`, `debug`). Unmapped values default to `info`.

```yaml
severity_mapping:
  ERR: error
  WRN: warning
  DBG: debug
  INF: info
```

---

## Example Patterns Reference

A typical system log configuration defines a set of prioritized patterns like this:

| Priority | Name | Event Type | What It Catches |
|---|---|---|---|
| 10 | `system_api_buffer` | command | Raw protocol/buffer lines — low-level data frames |
| 20 | `api_command_begin` | command | `Handler\|execute: Begin` — API call start |
| 21 | `api_command_end` | response | `Handler\|execute: End` — API call completion with return code |
| 30 | `system_dispatch_call` | command | Dispatch/transport call to downstream service or hardware |
| 35 | `system_dispatch_error` | error | Transport-level error code from downstream call |
| 40 | `api_err_code` | error | ERROR lines with structured codes (e.g. `ret_code=`, `error_code=`) |
| 45 | `api_err_command_error` | error | `execute: ERROR - [N]` — API-level error |
| 50 | `api_err_generic` | error | Any remaining ERROR lines |
| 60 | `system_warning` | warning | WARN lines |
| 70 | `system_lifecycle` | state_change | INIT/SHUTDOWN or begin/end function markers |
| 80 | `system_debug` | diagnostic | Extended debug/trace markers |
| 100 | `system_general` | info | Catch-all for everything else |

Command IDs, error codes, and response semantics are entirely protocol-specific — define your
own reference table in your project's config or docs for the API/system you're indexing.