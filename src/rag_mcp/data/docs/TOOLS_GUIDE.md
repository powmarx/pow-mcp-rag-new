# RAG MCP Tools Guide

This guide explains how to use the RAG search tools available in Kiro (or any MCP-compatible IDE).

You don't call these tools directly — just ask questions naturally and the AI will use the right tool automatically. But knowing what's available helps you ask better questions.

## Summary

| # | Tool | Purpose | Example prompt |
|---|------|---------|----------------|
| 1 | [`search_docs`](#1-search_docs--general-semantic-search) | General semantic search | "How does the dispense command work?" |
| 2 | [`search_specs`](#2-search_specs--documentation-only) | Search only documentation/specs | "What does the spec say about crypto steps?" |
| 3 | [`search_code`](#3-search_code--source-code-only) | Search only source/headers | "Find the implementation of StoreMoney" |
| 4 | [`search_hex_pattern`](#4-search_hex_pattern--hex-code-lookup) | Hex error code / packet ID lookup | "What is error 88a153?" |
| 5 | [`find_function`](#5-find_function--function-lookup) | Find function declarations + callers | "Find function CmdDispense" |
| 6 | [`find_variable`](#6-find_variable--variable-constant-lookup) | Find variable/constant/enum definitions and usage | "Find variable NOTE_HANDLING_DEPOSIT_REJECT" |
| 7 | [`get_document`](#7-get_document--retrieve-a-full-file) | Retrieve a full indexed file | "Show me server.py" |
| 8 | [`list_projects`](#8-list_projects--see-whats-indexed) | See what's indexed | "What projects are indexed?" |
| 9 | [`list_files`](#9-list_files--browse-project-files) | Browse files in a project | "List all headers in the project A project" |
| 10 | [`get_project_summary`](#10-get_project_summary--project-overview) | Quick project overview | "Give me a summary of the project A project" |
| 11 | [`compare_projects`](#11-compare_projects--side-by-side-comparison) | Side-by-side search across two projects | "Compare dispense in project A vs project B" |
| 12 | [`add_project`](#12-add_project--add-a-new-project) | Add + auto-detect + index a new project | "Add the CardReader project to the RAG" |
| 13 | [`add_file`](#13-add_file--index-a-specific-file) | Index a single file and persist for re-indexing | "Add this error codes CSV to the project A project" |
| 14 | [`add_folder`](#14-add_folder--index-a-folder) | Index all files in a folder and persist for re-indexing | "Add the tests folder to the project A project" |
| 15 | [`search_logs`](#15-search_logs--structured-log-search) | Search indexed log events with filtering | "Find ERR entries with error code 88a153 in the last hour" |
| 16 | [`index_log_file`](#16-index_log_file--index-a-log-file-on-demand) | Index a log file (or time window) on demand | "Index the last hour of device-26-04-28.log" |
| 17 | [`remove_project`](#17-remove_project--remove-a-project) | Remove a project and all its indexed data | "Remove the device logs project" |
| 18 | [`clear_project_index`](#18-clear_project_index--clear-indexed-data) | Clear indexed data but keep project config | "Clear the device logs index" |
| 19 | [`remove_file_from_index`](#19-remove_file_from_index--remove-a-files-chunks) | Remove a specific file's chunks from the index | "Remove device-26-04-28.log from the index" |

## Available Tools

### 1. search_docs — General semantic search

**What it does:** Searches all indexed content (code, docs, configs) using natural language.

**When to use:** General questions about your projects.

**Example prompts:**
- "How does the dispense command work?"
- "What happens when a cassette is full?"
- "Find code related to note handling information"

---

### 2. search_specs — Documentation only

**What it does:** Same as search_docs but filters to only documentation files (specs, requirements, design docs, PDFs). No source code noise.

**When to use:** When you want to know what the spec says, not how it's implemented.

**Example prompts:**
- "Find the requirements for cancel credit"
- "Search specs for new crypto feature"

---

### 3. search_code — Source code only

**What it does:** Searches only source files (.cpp, .c) and headers (.h). Filters out documentation.

**When to use:** When you want implementation details.

**Example prompts:**
- "Find the implementation of data layer status"
- "Show me header declarations for crypto states"

---

### 4. search_hex_pattern — Hex code lookup

**What it does:** Text-based search for hex error codes, packet IDs, or any hex pattern. Supports partial matches.

**When to use:** When you have a device error code or packet ID and need to know what it means.

**Example prompts:**
- "What is error 88a153?"
- "Look up packet 0x0521"
- "Search for hex pattern 88a152"
- "What does error code 88a15320 mean?"

**Tip:** For device codes like `88A153X0`, search the first 6 characters (e.g., "88a153") to find all variants.

---

### 5. find_function — Function lookup

**What it does:** Finds where a function is declared (headers) and where it's called (source). Also lists all files containing the function name.

**When to use:** When you need to understand a function's interface and who uses it.

**Example prompts:**
- "Find function CmdDispense"
- "Where is StoreMoney defined and called?"
- "Find all usages of BuildSetUnitInfoInputBuffer"

---

### 6. find_variable — Variable/constant lookup

**What it does:** Finds where a specific variable, constant, enum value, or #define is defined and used. Uses exact text matching.

**When to use:** When you have a specific identifier and need to see its definition and all files that use it.

**Example prompts:**
- "Find variable X"
- "Where is CASHED defined?"

---

### 7. get_document — Retrieve a full file

**What it does:** Returns the complete indexed content of a specific file.

**When to use:** When a search result looks relevant and you want to see the full file.

**Example prompts:**
- "Show me the full server.py file from the project A project"

---

### 8. list_projects — See what's indexed

**What it does:** Shows all indexed projects with their descriptions and chunk counts.

**When to use:** To see what knowledge is available.

**Example prompts:**
- "What projects are indexed in the RAG?"
- "List all available projects"

---

### 9. list_files — Browse project files

**What it does:** Lists all indexed files for a project, optionally filtered by type.

**When to use:** To see what files are searchable in a project.

**Example prompts:**
- "List all documentation files in the project A project"
- "What header files are indexed for my-project-b?"
- "Show me all config files in the project A project"

---

### 10. get_project_summary — Project overview

**What it does:** Shows a quick summary: description, file counts by type, total chunks.

**When to use:** To get a quick overview of a project's indexed content.

**Example prompts:**
- "Give me a summary of the project A project"
- "How much content is indexed for my-project-b?"

---

### 11. compare_projects — Side-by-side comparison

**What it does:** Searches for the same concept in two projects and shows results together.

**When to use:** When porting features between projects or understanding differences.

**Example prompts:**
- "Compare how dispense works in project A vs project B"
- "Show me the store money implementation in both projects"
- "Compare error handling between my-project-a and my-project-b"

---

### 12. add_project — Add a new project

**What it does:** Adds a new project to the RAG index. Auto-detects source patterns and indexes immediately.

**When to use:** When you want to add a new project without leaving the IDE.

**Example prompts:**
- "Add the CardReader project to the RAG, it's at C:/Users/me/GIT/products-API_CardReader"
- "Index the EPP project from C:/Users/me/GIT/products-API_EPP_4x"

---

### 13. add_file — Index a specific file

**What it does:** Indexes a single file into an existing project. The file path is saved to `config.yaml` so it gets re-indexed automatically on future runs.

**When to use:** When you want to add a specific file that isn't covered by the project's configured patterns.

**Example prompts:**
- "Add the file C:/path/to/error_codes.csv to the project A project"
- "Index C:/path/to/docs/protocol_notes.md into my-project-b"

---

### 14. add_folder — Index a folder

**What it does:** Indexes all matching files in a folder into an existing project. The folder and pattern are saved to `config.yaml` for automatic re-indexing.

**When to use:** When you want to add an entire directory of files to the index.

**Example prompts:**
- "Add the tests folder to the project A project"
- "Index the folder C:/path/to/project A/new-specs into my-project-a with pattern **/*.md"

**Note:** The folder must be inside the project's `base_path`. Default pattern is `**/*` (all files).

---

### 15. search_logs — Structured log search

**What it does:** Searches indexed log events with structured filtering and semantic search. Logs are parsed into structured events with metadata (severity, timestamp, error code, device ID, event type) so you can filter precisely.

**When to use:** When debugging device communication issues and you need to find specific log events by severity, time range, error code, or content.

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `query` | string (max 512 chars) | Semantic search on log message content. Optional if filters provided. |
| `project` | string | Filter by project name |
| `severity` | string | One of: DEBUG, INFO, WARNING, ERROR, CRITICAL |
| `time_range_start` | string | ISO 8601 start time (e.g., "2026-04-28T05:00:00Z") |
| `time_range_end` | string | ISO 8601 end time |
| `error_code_pattern` | string (max 32 chars) | Prefix match on error codes (e.g., "88a1" matches "88a15300") |
| `device_id` | string | Filter by device identifier |
| `event_type` | string | Filter by event type (command, response, error, state_change, etc.) |
| `top_k` | integer (1–50, default 20) | Maximum results to return |

**Example prompts:**
- "Search logs for errors with code AAA999"
- "Find all ERROR severity log events from today"
- "Find log events between 05:00 and 05:30 on April 28"
- "Show me all command events in the my_logs project"

**Tips:**
- Use `error_code_pattern` with a prefix to find all variants of an error code family
- Combine `severity: ERROR` with a time range to narrow down issues
- Use `query` alone for semantic search ("dispense failed") or combine with filters for precision
- If no `query` is provided but filters are set, results are ordered by timestamp (newest first)

---

### 16. index_log_file — Index a log file on demand

**What it does:** Indexes a specific log file (or time window within it) on demand. Processes the file in streaming batches so large files (100s of MB) are handled without memory issues. Progress is reported as a percentage.

**When to use:** When you need to index a large log file for debugging, either in full or just a specific time window of interest.

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `file` | string (required) | Log filename or glob pattern (e.g., "device-26-04-28.log") |
| `project` | string (default: "my_device_logs") | Project name containing log sources |
| `time_from` | string | Start time filter, HH:MM:SS format (e.g., "14:00:00") |
| `time_to` | string | End time filter, HH:MM:SS format (e.g., "15:30:00") |

**Example prompts:**
- "Index the log file device-26-04-28.log"
- "Index 2026-06-03.log between 10:00 and 11:00"

**Behavior:**
- Files are processed in 10k-line streaming batches (never loads the entire file into memory)
- Each batch goes through the full pipeline: parse → filter → transform → group → embed → store
- Already-stored chunks are preserved if interrupted — re-running is idempotent (upsert)
- Large log files (>5MB) are automatically skipped during background reindex and must be indexed via this tool
- Progress is logged every ~100k lines as a percentage

**Tips:**
- Use `time_from`/`time_to` for large files — a 576 MB file with a 1-hour window takes seconds
- Without a time window, a 576 MB file takes ~15-60 minutes depending on CPU speed
- **The tool blocks the MCP server while running** — no other tool calls are processed until it finishes
- To stop a running `index_log_file`, restart the MCP server (stored chunks are preserved)
- You can call this tool multiple times with different time windows — all chunks coexist
- If interrupted (server restart), just call again — progress is preserved (deterministic IDs)

---


### 17. remove_project — Remove a project

**What it does:** Removes a project entirely — deletes all indexed chunks from ChromaDB AND removes the project entry from config.yaml.

**When to use:** When you no longer need a project in the RAG index and want to clean up completely.

**Example prompts:**
- "Remove the my_device_logs project"
- "Delete the old test project from RAG"

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | string (required) | Project name to remove |

**Behavior:**
- Deletes all ChromaDB chunks for the project
- Removes the project from config.yaml (permanent)
- Returns confirmation with chunk count deleted

---

### 18. clear_project_index — Clear indexed data

**What it does:** Deletes all indexed chunks for a project but keeps the project configured in config.yaml. Ready for re-indexing.

**When to use:** When you want to re-index a project from scratch without reconfiguring it.

**Example prompts:**
- "Clear the device logs index"
- "Reset the project A project index"

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | string (required) | Project name to clear |

**Behavior:**
- Deletes all ChromaDB chunks for the project
- Does NOT modify config.yaml — project remains configured
- Use `index_log_file` or background reindex to re-populate

---

### 19. remove_file_from_index — Remove a file's chunks

**What it does:** Removes all indexed chunks for a specific file from a project's index. Supports exact path and partial/filename matching.

**When to use:** When you want to remove a large log file or outdated file from the index without clearing the entire project.

**Example prompts:**
- "Remove device-26-04-28.log from the device logs index"
- "Remove all XRV2-2026-05-14 files from the index"

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `file_path` | string (required) | Relative file path or filename (supports partial match) |
| `project` | string (required) | Project name |

**Behavior:**
- First tries exact `file_path` metadata match
- Falls back to partial/substring match if exact yields nothing
- Returns confirmation with chunks deleted count
- Does NOT modify config.yaml

---

## Tips

- **Be specific:** "How does CmdDispense handle full rejection cassettes?" works better than "dispense"
- **Use project names:** "Search the project A project for..." narrows results and is faster
- **Combine tools:** The AI often uses multiple tools — e.g., search_specs to understand the requirement, then search_code to find the implementation
- **Re-index after changes:** Click "Re-index RAG" in Agent Hooks, or the index updates automatically when Kiro restarts
- **Hex codes:** Always search the base pattern (first 6 chars) for device wildcard codes

## Project Names

Use the exact project name (as configured in `config.yaml`) when calling tools that take a
`project` parameter. Run `list_projects` to see all currently indexed projects and their names.
