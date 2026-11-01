# Observability Guide

The server and the indexer emit **structured JSON events** for tool calls, embedding failures
and log-epoch backfills, and keep **in-process counters** over them. Everything is stdlib-only:
no Prometheus, no agent, no daemon — one JSON line per event on stderr, the same line appended
to a JSONL file, and a `get_metrics` MCP tool / `rag-mcp get_metrics` CLI command to read the
numbers back.

Sections: [Where the log lives](#where-the-log-lives) · [Log format](#log-format) ·
[Counters](#counters) · [`get_metrics` MCP tool](#get_metrics-mcp-tool) ·
[`rag-mcp get_metrics` CLI](#rag-mcp-get_metrics-cli) · [Privacy, retention, limitations](#privacy-retention-limitations) ·
[Recipes](#recipes)

---

## Where the log lives

Every event is written to **two sinks**:

| Sink | When | Notes |
|---|---|---|
| **stderr** | always | MCP clients (Kiro, VS Code) capture the server's stderr; `docker logs` shows it in Docker mode. Replaces the old free-text `[tool] ...` lines. |
| **JSONL file** | when a path is known (see below) | Append-only, one JSON object per line. Shared by the server **and** the indexer, so a `rag-mcp index` run's embedding failures land in the same file the server writes to. |

The file path is resolved once per process, highest priority first:

1. `RAG_OBSERVABILITY_LOG_PATH` — explicit file path (env var).
2. `<storage.path>/observability/events.jsonl` — next to the ChromaDB data, where
   `storage.path` is the resolved `storage.path` from `config.yaml`.
3. Nothing — file sink disabled, stderr only. (Only happens in code paths that never boot the
   server or indexer, e.g. unit tests.)

Defaults per install mode:

| Install mode | Default events file |
|---|---|
| pip / uvx / `uv tool install` (Linux, macOS) | `~/.local/share/rag-mcp/observability/events.jsonl` |
| pip / uvx / `uv tool install` (Windows) | `%LOCALAPPDATA%\rag-mcp\observability\events.jsonl` |
| Docker | `/app/data/observability/events.jsonl` (inside the data volume, survives container restarts) |
| Repo checkout (`python server.py`) | `<repo>/data/observability/events.jsonl` (or wherever `storage.path` points) |

The server prints the resolved path on startup:

```
[startup] Event log: /home/me/.local/share/rag-mcp/observability/events.jsonl
```

If the file cannot be written (read-only volume, permissions), one
`[observability] WARNING: cannot append to ...` line is printed and the process carries on with
stderr only. Logging never breaks a tool call or an indexing run.

---

## Log format

One JSON object per line, no pretty-printing, UTF-8 written verbatim (no `\uXXXX` escapes).
Every record carries the same **envelope**:

| Field | Type | Meaning |
|---|---|---|
| `ts` | string | UTC timestamp, ISO 8601 with milliseconds: `2026-04-28T05:10:00.123Z` |
| `level` | string | `INFO` or `ERROR` |
| `event` | string | Event type: `tool_call`, `embed_failure` or `log_epoch_backfill` |
| `pid` | int | OS process id — the server and the indexer are separate processes writing to the same file |
| `message` | string | Human-readable one-line summary. Kept so `grep`/`tail` on stderr still reads naturally. |

Event-specific fields follow the envelope. Fields of other event types never appear.

### `tool_call` (level `INFO`)

Emitted by each MCP tool when it reports its result (via the `log_tool_call` helper).

| Field | Type | Meaning |
|---|---|---|
| `tool` | string | Tool name, e.g. `search_docs` |
| `params` | object | The parameters the tool reported. `query` is truncated to 100 characters (`"..."` appended). Other values are passed through as given. |
| `duration_ms` | float | Wall-clock time from the tool's start to the log call, one decimal |
| `result_count` | int | Results / chunks / files the tool reported (meaning is tool-specific, e.g. hits for a search, chunks for `add_file`) |

```json
{"ts":"2026-10-30T18:36:46.780Z","level":"INFO","event":"tool_call","pid":1282,"message":"search_logs(project='logs', severity='ERROR', top_k=20) -> 1 results in 35ms","tool":"search_logs","params":{"query":"","project":"logs","severity":"ERROR","top_k":20},"duration_ms":34.7,"result_count":1}
```

> **Coverage note.** A tool only emits `tool_call` where it calls `log_tool_call`. Early
> validation returns (`"Error: query must not be empty."`, `"Error: Project 'x' not found."`)
> emit nothing, exactly as before — so `tool_calls` counts are "calls that produced a result
> line", and there is no success/error outcome field yet.

### `embed_failure` (level `ERROR`)

Emitted from inside `EmbeddingGenerator` whenever an embedding call raises — during indexing,
log indexing **or** query encoding at search time. The exception is then re-raised unchanged, so
the caller's own error handling (and its own stderr message, e.g.
`[reindex] proj: failed to index bad.md: RuntimeError: boom`) is untouched; you will typically
see both lines.

| Field | Type | Meaning |
|---|---|---|
| `kind` | string | `documents` (indexing, log indexing, `add_file`/`add_folder`) or `query` (search-time query encoding) |
| `batch_size` | int | Number of texts in the failed batch (`1` for queries) |
| `model` | string | Embedding model name from `config.yaml` |
| `error_type` | string | Exception class name, e.g. `RuntimeError` |
| `error` | string | Exception message, truncated to 500 characters |

Deliberately **no** file path, project name or input text: the embedding layer does not know
what it is embedding, and query text is never persisted. Correlate with the surrounding
`[reindex] ...` stderr line when you need the file.

```json
{"ts":"2026-10-30T21:49:48.921Z","level":"ERROR","event":"embed_failure","pid":1696,"message":"embedding failed (kind=documents, batch_size=3, model=BAAI/bge-m3): RuntimeError: CUDA out of memory","kind":"documents","batch_size":3,"model":"BAAI/bge-m3","error_type":"RuntimeError","error":"CUDA out of memory"}
```

### `log_epoch_backfill` (level `INFO`)

Log chunks indexed by older versions only carry ISO-string timestamps, which ChromaDB cannot
range-filter. The first time-filtered `search_logs` on such a collection backfills numeric
`ts_start_epoch` / `ts_end_epoch` fields in place (once per collection per process). This event
is emitted **only when at least one record was updated** — a check that finds nothing to do is
silent.

| Field | Type | Meaning |
|---|---|---|
| `collection` | string | ChromaDB collection name, e.g. `rag_logs` (`<collection_prefix>_<project>`) |
| `records_checked` | int | Log chunks in the collection |
| `records_missing` | int | Chunks that had no numeric epoch fields |
| `records_updated` | int | Chunks that received them. `missing − updated` = legacy chunks whose timestamps don't parse; they stay "missing" on every run and are excluded from time-range queries. |
| `duration_ms` | float | Wall-clock time of the whole backfill, one decimal |

```json
{"ts":"2026-10-30T22:35:57.508Z","level":"INFO","event":"log_epoch_backfill","pid":2052,"message":"backfilled epoch timestamps on 3/3 legacy log chunks in rag_logs (3 checked) in 116ms","collection":"rag_logs","records_checked":3,"records_missing":3,"records_updated":3,"duration_ms":115.9}
```

---

## Counters

Every emitted event is also folded into **in-process counters**. They are exact for the process
that holds them, live in memory only, and reset when the process exits.

| Section | Counter | Meaning |
|---|---|---|
| `tool_calls` | `total` | Number of `tool_call` events |
| | `by_tool.<tool>.count` | Calls per tool |
| | `by_tool.<tool>.total_duration_ms` / `avg_duration_ms` / `max_duration_ms` | Duration statistics per tool |
| | `by_tool.<tool>.zero_result_calls` | Calls that reported `result_count == 0` — a cheap "is search returning nothing?" signal |
| `embed_failures` | `total` | Number of `embed_failure` events |
| | `by_kind` | Split `documents` / `query` |
| | `by_error_type` | Split by exception class |
| `log_epoch_backfills` | `events` | Number of `log_epoch_backfill` events. Because only backfills that updated something emit, this is **"backfills with updates"**, not "checks performed". |
| | `records_updated_total` | Sum of `records_updated` |
| | `by_collection.<name>.events` / `.records_updated` | The same, per collection |

There are two ways to read them, and they answer different questions:

| | `get_metrics` MCP tool | `rag-mcp get_metrics` CLI |
|---|---|---|
| Source | **Live** counters of the running server process | **Replay** of the JSONL file from disk |
| Scope | This server process, since it started | Everything in the file: server *and* indexer runs, across restarts |
| Needs | A running server (it *is* the server) | Only the file — works when no server is running |
| Misses | Anything from other processes (e.g. an `rag-mcp index` embedding failure) | Anything not written to the file (file sink disabled/unwritable) |
| Extra block | `process` — `pid`, `started_at`, `uptime_seconds` | `file` — `path`, `exists`, `lines_total`, `lines_parsed`, `lines_skipped`, `first_ts`, `last_ts`, `process_count` |
| `source` value | `"live"` | `"jsonl_replay"` |

Both produce the same `tool_calls` / `embed_failures` / `log_epoch_backfills` sections with
identical semantics — they are computed by the same code from the same events — so the two
outputs can be diffed. A difference means "events from other processes" or "events that did not
reach the file", nothing else.

---

## `get_metrics` MCP tool

Returns the live counters of the server process as a pretty-printed JSON document. Takes no
parameters. Ask the assistant things like "show me the RAG server metrics" or "how many
embedding failures since the server started?".

```json
{
  "source": "live",
  "process": {
    "pid": 2550,
    "started_at": "2026-10-31T22:13:31Z",
    "uptime_seconds": 812.4
  },
  "tool_calls": {
    "total": 3,
    "by_tool": {
      "search_logs": {
        "count": 3,
        "total_duration_ms": 31.9,
        "avg_duration_ms": 10.6,
        "max_duration_ms": 26.1,
        "zero_result_calls": 1
      }
    }
  },
  "embed_failures": {
    "total": 1,
    "by_kind": {"query": 1},
    "by_error_type": {"RuntimeError": 1}
  },
  "log_epoch_backfills": {
    "events": 1,
    "records_updated_total": 3,
    "by_collection": {"rag_logs": {"events": 1, "records_updated": 3}}
  }
}
```

Calling `get_metrics` is **not itself counted or logged** as a `tool_call` — metrics reads would
otherwise inflate the numbers they report and fill the file with self-observations.

---

## `rag-mcp get_metrics` CLI

Replays the JSONL file and prints a report. Does not start the server, load any model or open
ChromaDB. Exit code is `0` even when the file does not exist yet.

```bash
rag-mcp get_metrics                    # human-readable report
rag-mcp get_metrics --json             # raw JSON, same sections as the MCP tool
rag-mcp get_metrics --file PATH        # replay a specific file (a copy, a rotated file, another machine's)
```

| Flag / variable | Effect |
|---|---|
| `--json` | Print the snapshot as JSON instead of the text report. |
| `--file PATH` | Replay this file. Highest priority. |
| `RAG_OBSERVABILITY_LOG_PATH` | Used when `--file` is absent. Same variable the server/indexer honour, so setting it once keeps writer and reader in sync. |
| *(neither)* | `<storage.path>/observability/events.jsonl`, resolved from `config.yaml` the same way the server does (`RAG_CONFIG_PATH` / `RAG_DATA_PATH` are honoured). |

Text report:

```
$ rag-mcp get_metrics
Event log : /tmp/obs-demo/events.jsonl
Events    : 6 parsed, 0 skipped (malformed/unknown), 2 process(es)
Span      : 2026-10-31T22:13:31.994Z -> 2026-10-31T22:13:32.353Z

Tool calls: 3
  tool                      calls    avg ms    max ms  zero-result
  search_logs                   3      10.6      26.1            1

Embed failures: 2
  by kind      : documents=1, query=1
  by error type: RuntimeError=1, ValueError=1

Log-epoch backfills: 1 event(s), 3 record(s) updated
  rag_logs                 1 event(s), 3 updated
```

(This is the same session as the MCP tool example above, plus one `rag-mcp index` run in a second
process — hence `2 process(es)` and `Embed failures: 2` vs the tool's `1`.)

JSON report — identical sections, `file` instead of `process`:

```json
{
  "source": "jsonl_replay",
  "file": {
    "path": "/tmp/obs-demo/events.jsonl",
    "exists": true,
    "lines_total": 6,
    "lines_parsed": 6,
    "lines_skipped": 0,
    "first_ts": "2026-10-31T22:13:31.994Z",
    "last_ts": "2026-10-31T22:13:32.353Z",
    "process_count": 2
  },
  "tool_calls": { "...": "same shape as the MCP tool" },
  "embed_failures": { "...": "..." },
  "log_epoch_backfills": { "...": "..." }
}
```

Replay rules:

- Blank lines are ignored. Lines that are not valid JSON (e.g. a line truncated by a crash
  mid-write), not a JSON object, or have an unknown `event` are **skipped and counted** in
  `lines_skipped` — never fatal, so a file from a newer version with extra event types still
  replays.
- A missing file prints `(file not found — nothing has been logged yet)` and all-zero counters.
- Replaying never touches the live counters of any process.

---

## Privacy, retention, limitations

- **What is persisted.** Tool parameters (`query` truncated to 100 chars, project names, file
  paths as passed to the tool), embedding error messages (truncated to 500 chars), collection
  names, timings. Never document content, never query embeddings, and `embed_failure` carries no
  file/project/query context at all. Treat the file like any other application log: it lives
  next to the index data and is not redacted.
- **No rotation.** The file grows without bound. Truncate or rotate it externally
  (`logrotate`, a scheduled task, or simply delete it — the next event recreates it). Counters in
  a running server are unaffected by truncating the file.
- **No cross-process locking.** Each event is one `write()` of one line in append mode; the
  server and indexer may append concurrently. On Linux/macOS this is safe for lines well under
  the page size (ours are a few hundred bytes). A crash mid-write can leave one truncated line,
  which the replay skips.
- **Counters are not persisted.** Restarting the server resets the MCP tool's numbers; use the
  CLI for history.
- **Not a metrics endpoint.** There is no `/metrics`, no push, no sampling. If you need
  Prometheus-style scraping later, the JSONL file is the thing to feed it from.

---

## Recipes

```bash
# Follow tool calls as they happen (server stderr in Docker)
docker logs -f rag-mcp 2>&1 | grep '"event":"tool_call"'

# All embedding failures, newest first, with jq
tac ~/.local/share/rag-mcp/observability/events.jsonl | jq -c 'select(.event=="embed_failure") | {ts,kind,error_type,error}'

# Slowest 10 tool calls
jq -c 'select(.event=="tool_call") | [.duration_ms,.tool,.params]' events.jsonl | sort -rn | head

# Calls that returned nothing
jq -c 'select(.event=="tool_call" and .result_count==0) | {ts,tool,params}' events.jsonl

# Which processes wrote to the file, and how many lines each
jq -r .pid events.jsonl | sort | uniq -c

# Snapshot the counters for a bug report
rag-mcp get_metrics --json > metrics-$(date +%F).json
```
