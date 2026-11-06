# Plan: observability hardening

Status: **proposed, nothing implemented.** Output of a code assessment of the observability
layer on 2026-11-06. Items are ordered by priority; IDs (`P0-1`, `P1-3`, ...) are stable so
commits and PRs can reference them.

## Scope assessed

| Area | Files |
|---|---|
| Core | `src/rag_mcp/observability.py` (events, counters, replay, text report) |
| Emitters | `tools/helpers.py` (`ToolCall`), `embedding_generator.py`, `chroma_store.py` (`backfill_log_epochs`) |
| Readers | `tools/metrics.py` (`get_metrics`), `cli.py` (`rag-mcp get_metrics`) |
| Wiring | `server.py`, `_server.py`, `indexer.py`, `_indexer.py`, `docker-compose.yml` |
| Not instrumented but relevant | `tools/search.py`, `reranker.py`, `indexing_pipeline.py`, `log/log_indexer.py`, `file_reader.py`, `pdf_*.py` |
| Docs / tests | `doc/OBSERVABILITY.md`, `tests/observability/*` |

## How findings were checked

Items marked **[verified]** were reproduced by running a throwaway script against the code in
`.venv` (results in the evidence column). Items marked **[read]** come from reading the code
only and have not been run.

## Summary

The design is sound: stdlib only, one JSON line per event, one counter class shared by live and
replay paths, and a `ToolCall` helper that makes "every return emits exactly once" checkable.
The main problems are that the layer can break the code it observes (P0-1), cannot see the
failures that matter most (P0-2, P0-3), and has several robustness and scale gaps (P1).
Enhancements (P2, P3) are mostly about attributing latency and covering indexing.

| Priority | Meaning | Items |
|---|---|---|
| **P0** | Observability breaks behaviour, or hides real failures. Fix first. | 3 |
| **P1** | Wrong or unreliable data, or unbounded growth. Fix next. | 7 |
| **P2** | Missing signals that make diagnosis slow. | 7 |
| **P3** | Nice to have, or only matters for specific deployments. | 6 |

---

## P0 — fix first

### P0-1. `emit()` can raise into the caller, contradicting "never breaks the caller" [verified]

- **Where:** `observability.emit()`. Only the file write is wrapped in `try/except OSError`.
  `_counters.ingest(record)` and `print(line, file=sys.stderr)` are not.
- **Evidence:** replacing `sys.stderr` with an object whose `write` raises `BrokenPipeError`
  makes `log_tool_call_event(...)` raise `BrokenPipeError`. In stdio mode the client owns the
  stderr pipe, so a client that closes or stops reading it turns every tool call into an error
  (the log call sits before `return text` in `ToolCall.ok/error`).
- **Second effect:** in `EmbeddingGenerator._encode`, the `except` block calls
  `log_embed_failure_event` before `raise`. If logging raises, the original embedding
  exception is replaced by the logging exception.
- **Fix:** wrap the stderr write and the counter ingest in `emit()` in `try/except Exception`
  (stderr failure: warn once to the file sink or swallow; ingest failure: swallow). Keep the
  one-time warning pattern used for the file sink.
- **Done when:** a test with a raising `sys.stderr` and a test with a raising `ingest` both show
  the tool still returns its text and `_encode` still re-raises the original exception.
- **Effort:** S.

### P0-2. Exceptions that escape a tool body emit nothing [read; known gap, "Part 4" of `tool-call-outcome`]

- **Where:** `ToolCall` only emits from `.ok()` / `.error()`. An unhandled exception in a tool
  body (for example from `_ctx.store.list_collections()` in `search_docs`, which is outside any
  `try`) reaches the client as an MCP protocol error with no `tool_call` event, no counter,
  and no stderr line from this layer.
- **Why P0:** these are the genuine server faults. Everything the layer reports today is
  mostly client-input errors, so the most important failure class is the one not counted.
- **Fix:** give `ToolCall` an `unhandled(exc)` path (outcome `error`, `error_type`, truncated
  message) and apply it in one place: a decorator on each `_xxx_sync` implementation, or a
  wrapper in each module's `register()`. Re-raise unchanged. Add `error_type` to the
  `tool_call` event and an `unhandled_calls` counter so these are separable from returned error
  texts.
- **Done when:** a test makes `store.list_collections` raise, asserts one `tool_call` event with
  `outcome="error"` and `error_type`, and asserts the exception still propagates. The existing
  guard test ("every `return` is `call.ok/error`") stays green.
- **Effort:** M.

### P0-3. Swallowed exceptions in search turn failures into "ok" results [read]

- **Where:** `tools/search.py`.
  - `_search_docs_sync`: `except Exception: continue` around `collection.query(...)` and around
    `client.get_collection(...)`. If every collection fails, the call returns
    `No results found for query` with `outcome="ok"`, `result_count=0`. It is counted in
    `zero_result_calls`, the metric documented as "is search returning nothing?".
  - Same pattern in `search_hex_pattern` and `find_variable` (bare `except Exception: continue`
    in the collection loops).
  - Reranker failure: `print(f"[search] Reranking failed ...", file=__import__("sys").stderr)`.
    Free text, not an event, not counted. A permanently broken reranker silently degrades
    ranking quality for every query.
- **Contrast:** `search_logs` already collects per-collection errors (`errors.append(...)`),
  so there is a local precedent.
- **Fix:**
  1. New event `search_degraded` (level `WARNING`, see P2-5): `tool`, `stage`
     (`collection_query` | `get_collection` | `rerank`), `collection`, `error_type`, `error`.
     Emit it from each swallowed `except`.
  2. Add a `degraded_calls` counter per tool (call finished `ok` but at least one stage failed).
  3. If **all** collections failed, return `call.error(...)` instead of an empty `ok`.
- **Done when:** a test where `collection.query` raises for every collection yields
  `outcome="error"`; a test where one of two collections raises yields `ok` plus one
  `search_degraded` event and `degraded_calls == 1`; a test where `rerank` raises yields a
  `search_degraded(stage="rerank")` event.
- **Effort:** M.

---

## P1 — next

### P1-1. Durations use the wall clock, not a monotonic clock [verified]

- **Where:** `ToolCall.__init__` stores `time.time()`; `log_tool_call_event` and
  `log_log_epoch_backfill_event` subtract it.
- **Evidence:** passing a `start_time` 5 s in the future yields `duration_ms = -5000.0`. An NTP
  step or a VM resume during a call gives negative or inflated durations, which then corrupt
  `total_duration_ms`, `avg_duration_ms` and `max_duration_ms`.
- **Fix:** use `time.perf_counter()` for elapsed time (store a perf-counter start in
  `ToolCall`; keep `time.time()` only for the `ts` field). Clamp to `>= 0`. Keep the public
  `start_time` parameter working for existing tests, or change the tests together with the code.
- **Effort:** S.

### P1-2. Replay crashes on a valid JSON line with a wrong-typed field [verified]

- **Where:** `_Counters.ingest`: `float(record.get("duration_ms") or 0.0)` and
  `int(record.get("records_updated") or 0)`.
- **Evidence:** the line `{"event":"tool_call","tool":"x","duration_ms":"abc"}` makes
  `aggregate_events_from_log` raise `ValueError`. `doc/OBSERVABILITY.md` states replay is
  "never fatal", and only `JSONDecodeError` is caught. The CLI is the tool people reach for
  when something is already wrong, so it should survive a damaged file.
- **Fix:** coerce through a small `_num()` helper that returns `0.0` / `0` on failure, or catch
  `(TypeError, ValueError)` around `ingest` in the replay loop and count the line as skipped.
  The second option also covers future fields.
- **Effort:** S.

### P1-3. Stderr lines can interleave across threads [verified]

- **Where:** `emit()` uses `print(line, file=sys.stderr)`, which performs two writes: the text
  and the `"\n"` (observed: `['...m"}', '\n']`). Tools run on anyio worker threads, so two
  events can produce `{...}{...}\n\n`. The file sink is protected by `_file_lock`; stderr is not.
- **Effect:** a consumer that parses stderr as JSON-per-line (the documented `docker logs | grep
  '"event":"tool_call"'` recipe, or any log shipper) can see a broken line.
- **Fix:** write `line + "\n"` in a single `sys.stderr.write` call, and hold a lock around it
  (reuse a second lock, not `_file_lock`, so a slow disk cannot block stderr).
- **Effort:** S.

### P1-4. Only `query` is bounded; other parameters are written unbounded [verified]

- **Where:** `_sanitize_params` truncates only the key `query`. `_format_params` builds
  `message` from `repr` of every other value.
- **Evidence:** a 5,000-character `pattern` value produced a 5,043-character `message` and a
  5,000-character `params.pattern`. Candidates: `add_pattern.pattern`, `index_log_file.file`,
  `add_folder.pattern`, `function_name`, `variable_name`, paths.
- **Why it matters:** one line can be arbitrarily large, which defeats the "a few hundred
  bytes per line" assumption behind the append-safety claim in the doc, and inflates the log.
  It also persists whatever a caller passes, including secrets pasted into a path or pattern.
- **Fix:** truncate every string param to a single limit (for example `MAX_PARAM_CHARS = 200`),
  cap `repr` output in `message`, and cap the whole serialized line (for example 8 KB) with a
  `truncated: true` marker. Keep `query` at 100.
- **Effort:** S.

### P1-5. No rotation or retention; replay cost grows with the file [read]

- **Where:** `_write_file_line` appends forever. Documented as "No rotation", which makes it a
  known limitation, but every `tool_call` of every tool is logged at INFO, so growth is
  proportional to usage, and `rag-mcp get_metrics` re-reads the whole file each time.
  In Docker the file lives in the data volume next to the index.
- **Fix:** size-based rotation in the writer (for example 10 MB, keep 5:
  `events.jsonl`, `events.jsonl.1`, ...), done under `_file_lock`. Make limits configurable
  (`RAG_OBSERVABILITY_MAX_BYTES`, `RAG_OBSERVABILITY_BACKUPS`). Let the CLI replay rotated
  files too (`--include-rotated`, default on).
- **Cross-process caveat:** the server and indexer both write. Rotation by rename is not safe
  across processes on Windows (open handles block the rename). Either rotate only at process
  start, or accept that a failed rotation is skipped and retried later. Decide this in the
  design step.
- **Effort:** M.

### P1-6. A process is identified only by `pid`; `process_count` is wrong in Docker [read]

- **Where:** envelope has `pid`; replay computes `process_count = len(set(pids))`.
- **Why it matters:** `docker-compose.yml` runs `python server.py` as the container command, so
  the server is very likely PID 1 on every restart. A file covering ten restarts then reports
  `1 process(es)`. PIDs are also reused on any OS over a long enough period.
- **Fix:** add `run_id` (short random id generated at import or `configure()`), `component`
  (`server` | `indexer`), and `version` to the envelope. Count distinct `run_id` in replay
  (fall back to `pid` for old records). This also gives the start boundary needed for P2-4.
- **Effort:** S.

### P1-7. Nested calls cannot be tied back to the client request [read]

- **Where:** `find_function`, `compare_projects`, `search_code`, `search_specs` call
  `_search_docs_sync`, which emits its own `search_docs` events. The doc says
  `tool_calls.total` counts "implementations invoked, not client requests". `search_specs` and
  `search_code` never appear under their own name, so usage by entry point is invisible.
- **Fix:** a `contextvars`-based call id: the outermost `ToolCall` sets `call_id`, nested ones
  record `parent_id` and `entry_tool`. Add both to the event. Counters can then expose
  `client_requests` (events with no parent) next to `total`. Alternatively, give
  `search_specs` and `search_code` their own `ToolCall` and mark the inner `search_docs`
  events as `nested: true`.
- **Note on a related defect:** `_search_code_sync` decides "no results" by substring match on
  `"No results"` in the returned text. A result chunk whose content contains that phrase, or an
  error text, is misclassified. Returning a structured `(count, text)` from `_search_docs_sync`
  would fix both the correlation and the substring check.
- **Effort:** M.

---

## P2 — enhancements

### P2-1. No stage timings inside a search
`tool_call` has one `duration_ms`. A slow `search_docs` cannot be attributed to model load,
query encoding, vector query, or reranking. Add optional `stages` (object of ms) to the event:
`ensure_model`, `encode`, `query`, `rerank`. Also record `cold_start: true` when the call
triggered a lazy model load, which explains the first-call outlier in `max_duration_ms`.

### P2-2. Indexing is not observable beyond `embed_failure`
`indexing_pipeline.py` reports per-file failures, progress and cancellation with free-text
`print`. The indexer is a separate process and its only structured event is `embed_failure`
(which, by design, has no file or project). Add events: `index_run` (project, files, chunks,
skipped, failed, duration, cancelled), `index_file_failure` (project, relative path,
`error_type`, truncated `error`), and `index_prune` (pruned count). Counters: files indexed,
files failed, chunks written per project. This also lets `embed_failure` be correlated
through `run_id` (P1-6) and the surrounding `index_file_failure`.

### P2-3. Model lifecycle and reranker events
Embedding and reranker loads are printed (`[startup] Embedding model loaded in ...ms`) and not
counted. Add `model_load` (kind `embedding` | `reranker`, model, `duration_ms`, outcome).
Add `rerank` timing and failure to P0-3's events. Today there is no signal that the reranker
is configured but failing.

### P2-4. Counters are all-time per process; no time windows or percentiles
Counters give `count`, total, average and max. Add: `last_call_ts` and `last_error` (tool, ts,
text) per tool, a fixed-bucket latency histogram (or p50/p95 from a bounded reservoir) per
tool, and `--since` / `--until` / `--tool` filters on the CLI replay. Averages are already
skewed by sub-millisecond validation errors (documented in D4), so percentiles over `ok` calls
only would be more useful than the current average.

### P2-5. Levels and sink control
Only `INFO` and `ERROR` exist, and every event goes to both sinks with no way to turn either
off. Add `WARNING` (for P0-3, file-sink failure), `RAG_OBSERVABILITY_LEVEL`, and
`RAG_OBSERVABILITY_STDERR=0|1` / `RAG_OBSERVABILITY_FILE=0|1`. Reason: a busy client prints
one JSON line per call in its MCP log panel, and some users will want the file only.

### P2-6. Per-project dimension
Counters are per tool only. Add `by_project` for calls that carry a `project` param
(search, list, add, remove). This answers "which project is returning zero results".

### P2-7. Unstructured stderr is mixed with JSON
Many modules still `print` with inconsistent prefixes (`[error]`, `[warning]`, `WARNING:`,
`[search_logs] Error ...`, `[remove_project] Error deleting collection`). The stream is
therefore not purely JSON, so a consumer that parses stderr line by line must tolerate
non-JSON lines. Route the error and warning prints in
`file_reader.py`, `pdf_extractor.py`, `pdf_converter.py`, `log/log_indexer.py`,
`tools/logs.py` and `tools/management.py` through `emit(..., level="WARNING"|"ERROR")`.
Keep the human-oriented `[startup]` lines as they are, or document them as the only
non-JSON lines.

---

## P3 — optional

| ID | Item | Note |
|---|---|---|
| P3-1 | Prometheus / OpenTelemetry export | `get_metrics` is JSON only. The snapshot shape maps directly to Prometheus text exposition; ship it as an optional extra, not a core dependency (current design is stdlib only). |
| P3-2 | `/health` and `/metrics` in HTTP mode | `--http` binds `0.0.0.0` and `docker-compose.yml` defines no healthcheck. A `/health` route would let Docker restart a wedged server. If `/metrics` is added, decide on auth first. |
| P3-3 | One `configure_from_config()` for all entry points | The same two lines (`configure(default_log_path(...))`) are duplicated in `server.py`, `_server.py`, `indexer.py`, `_indexer.py`. Drift risk when P1-5, P1-6 and P2-5 add settings. |
| P3-4 | Cheaper file sink | The file is opened and `mkdir`'d on every event. Acceptable now; if tool-call volume rises, keep the handle open per process with flush-per-line. After a failure the writer retries on every event even though the warning says "file sink disabled", so the message and the behaviour disagree. Align them. |
| P3-5 | File permissions and redaction | The log holds queries and absolute paths and is created with default permissions. Create with `0600` on POSIX, and offer a `RAG_OBSERVABILITY_REDACT_QUERIES=1` mode that stores a hash and length instead of the query text. |
| P3-6 | Windows append semantics | The doc's claim that concurrent appends from server and indexer are safe is stated for Linux/macOS. Verify on Windows with a two-process stress test, or document it as unverified. |

### Side finding (not observability code)

`docker-compose.yml`, service `server`: the volume line reads `- -rag-mcp-new-pip-data:/app/data`
(extra leading `-`). That is not the named volume used by the other services, and a name
starting with `-` is not a valid volume name. As written, the server service would not share
the data volume (and therefore the event log) with `indexer` and `server-http`. Fix the typo
separately. [read]

---

## Suggested delivery

| Step | Items | Why this order |
|---|---|---|
| 1 | P0-1, P1-1, P1-2, P1-3, P1-4 | Small, local to `observability.py`, no schema change except the optional `truncated` marker. One PR. |
| 2 | P0-2, P0-3 | Adds `error_type`, `unhandled_calls`, `degraded_calls` and the `search_degraded` event. Update `doc/OBSERVABILITY.md` and its bundled copy in `src/rag_mcp/data/docs/` (via `scripts/sync_package_data.py`); `tests/observability/test_docs.py` enforces both. |
| 3 | P1-6, P1-7, P1-5 | Envelope additions (`run_id`, `component`, `version`, `call_id`, `parent_id`), then rotation, which benefits from `run_id`. |
| 4 | P2-1 to P2-7 | Independent of each other; order by what users ask for first. P2-2 (indexing events) has the most value. |
| 5 | P3 | On demand. |

## Compatibility rules for every step

- Replay must keep accepting old files: new fields are optional, unknown events and unknown
  values are skipped or treated as `ok` (decision D5 of `tool-call-outcome`).
- `get_metrics` snapshot keys only grow; no existing key changes meaning.
- `get_metrics` stays unlogged as a `tool_call`.
- New events get a section in `doc/OBSERVABILITY.md` and a case in `tests/observability/`.
- The `ToolCall` guard test ("every `return` in a tool body is `call.ok/error`") must stay
  green; P0-2 should extend it, not bypass it.

## Not verified in this assessment

- Behaviour on Windows under concurrent server and indexer writes (P3-6).
- Whether the server is PID 1 in the published Docker image (P1-6); inferred from the compose
  `command`.
- The cost of the per-event file open under real load (P3-4); no benchmark was run.
- The full test suite was not run; only the throwaway probes listed above were executed.
