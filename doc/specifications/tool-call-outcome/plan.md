# Plan: tool-call outcome coverage

Status: **plan only — nothing implemented yet.**

## Problem

`tool_call` events are emitted only where a tool body reaches its `log_tool_call(...)` line. Two
consequences:

1. **Errors emit nothing.** An AST pass over the 19 tool bodies in `src/rag_mcp/tools/` finds
   **75 early `return "Error: ..."` paths** that produce no event at all (table in Part 2). A
   client hammering `search_docs` with an unknown project, or `add_file` with a bad path, is
   invisible in both the JSONL file and `get_metrics`.
2. **The few errors that do emit look like empty successes.** `get_document` (project/document
   not found) and `list_files` (project not found, ChromaDB query raised) call `log_tool_call`
   with `result_count=0`, so they are folded into `zero_result_calls` — the "is search returning
   nothing?" signal is polluted by failures.

`doc/OBSERVABILITY.md` currently documents this gap in a "Coverage note" under `tool_call`.

## Scope

| Part | What | Files |
|---|---|---|
| 1 | `outcome` (+ `error`) on the `tool_call` event; `error_calls` / `errors` in the counters, snapshot, text report | `observability.py`, `tools/helpers.py`, `cli.py` (text only) |
| 2 | Route every early return through a helper so all 75 paths emit `outcome="error"`; reclassify the 4 misreported ones | `tools/search.py`, `tools/documents.py`, `tools/management.py`, `tools/logs.py` |
| 3 | Docs + bundled copy + doc tests | `doc/OBSERVABILITY.md`, `doc/CLI_REFERENCE.md`, `doc/TOOLS_GUIDE.md`, `src/rag_mcp/data/docs/OBSERVABILITY.md`, module docstring |
| 4 (optional, separable) | Exceptions that *escape* a tool body (today: MCP error to the client, no event) | `tools/helpers.py`, the four tool modules' `register()` |

Out of scope (called out so they are not accidentally picked up): nested events
(`find_function` / `search_code` / `compare_projects` call `_search_docs_sync`, which emits its
own `search_docs` events — existing behaviour, unchanged); `compare_projects` always reporting
`result_count=0`; per-file failures inside `index_log_file` (they are summarised in the result
text, the call as a whole is still `ok`); `get_metrics` itself (stays unlogged by design).

---

## Decisions (confirm before implementation)

| # | Decision | Rationale / alternative |
|---|---|---|
| D1 | `outcome` is a two-valued enum: `"ok"` \| `"error"`. | Smallest thing that fixes the problem and keeps counters trivial. Alternative considered: a third `"cancelled"` for `index_log_file` — only one tool can produce it and the result text already says "cancelled"; can be added later without breaking anything because replay treats unknown values as `ok` (D5). **Not added.** |
| D2 | `level` stays `INFO` for every `tool_call`, including errors. | Nearly all tool errors are *client-input* errors ("project is required", "not found"). Flagging them `ERROR` would make `grep '"level":"ERROR"'` useless for spotting real server faults, which `embed_failure` already covers. Filter on `"outcome":"error"` instead. |
| D3 | `error` field = the exact text returned to the client, truncated to `MAX_ERROR_CHARS` (500, same constant as `embed_failure`). Present **only** when `outcome == "error"`. | One source of truth (what the client saw). `message` gets the first line only, so stderr stays one line per event. |
| D4 | Duration stats (`total/avg/max_duration_ms`) and `count` stay "all calls". `error_calls` and `zero_result_calls` are **disjoint subsets** of `count`: `zero_result_calls` now counts only `outcome == "ok"` calls with `result_count == 0`. | This is the actual fix for "a failure looks like an empty call". Side effect to be aware of: sub-millisecond validation errors pull `avg_duration_ms` down; documented, not worked around. |
| D5 | Replay of a record **without** `outcome` (files written before this change) is treated as `ok`. | Keeps `rag-mcp get_metrics` working on existing files; those records were only ever emitted on (mostly) success paths. Unknown `outcome` values are also treated as `ok`, so a future enum extension replays on old binaries. |
| D6 | `params` are captured **once, at tool entry, as received** (before `strip()` / clamping). | Needed so an early return can emit the same params as a late one. Visible change: `search_docs` / `search_logs` will log the *requested* `top_k` (e.g. `500`) instead of the clamped value — arguably more useful (shows the misbehaving client). Documented. |
| D7 | Call sites move to a tiny `ToolCall` helper (`call.ok(n, text)` / `call.error(text)`), both returning the text so the tool body does `return call.error("Error: ...")`. | 75 sites: the one-object-per-call form is the only one that keeps each site to a one-line diff **and** makes completeness machine-checkable (Part 2 guard test: *every `return` in a tool body is `call.ok(...)` or `call.error(...)`*). `log_tool_call()` keeps its signature (extended with keyword args) so existing tests and the helper share one emit path. |

---

## Part 1 — `outcome` on the event and in the counters

### 1.1 Event (`observability.py`)

`log_tool_call_event(tool, params, start_time, result_count, outcome="ok", error=None)`

- Validate `outcome in {"ok", "error"}` (fall back to `"ok"` — emit must never raise).
- On `"error"`: force `result_count` to `0`, add `error=_truncate(error or "", MAX_ERROR_CHARS)`.
- `message`:
  - ok: unchanged — `search_docs(query='x') -> 3 results in 12ms`
  - error: `search_docs(query='x') -> error in 0ms: Error: query must not be empty.`
    (first line of `error` only; multi-line texts exist, e.g. `add_project` rollback messages)

Resulting record (error case):

```json
{"ts":"...","level":"INFO","event":"tool_call","pid":1282,
 "message":"search_docs(query='x', project='nope', top_k=5, file_type='') -> error in 0ms: Error: Project 'nope' not found. Use list_projects() to see available projects.",
 "tool":"search_docs","params":{"query":"x","project":"nope","top_k":5,"file_type":""},
 "duration_ms":0.3,"result_count":0,"outcome":"error",
 "error":"Error: Project 'nope' not found. Use list_projects() to see available projects."}
```

Field order: `outcome` directly after `result_count`; `error` last (keeps `grep`/`jq` on existing
fields stable).

### 1.2 Helper (`tools/helpers.py`)

```python
def log_tool_call(tool_name, params, start_time, result_count, outcome="ok", error=None):
    observability.log_tool_call_event(tool_name, params, start_time, result_count, outcome, error)

class ToolCall:
    """One per tool invocation. Captures name, params and start time at entry."""
    def __init__(self, tool: str, params: dict):
        self.tool, self.params, self.start_time = tool, params, time.time()
    def ok(self, result_count: int, text: str) -> str:
        log_tool_call(self.tool, self.params, self.start_time, result_count)
        return text
    def error(self, text: str) -> str:
        log_tool_call(self.tool, self.params, self.start_time, 0, outcome="error", error=text)
        return text
```

### 1.3 Counters (`_Counters.ingest` / `snapshot`)

```
tool_calls:
  total:   <all tool_call events>
  errors:  <events with outcome == "error">            # NEW
  by_tool.<tool>:
    count, total_duration_ms, avg_duration_ms, max_duration_ms   # unchanged, all calls
    error_calls:        outcome == "error"               # NEW
    zero_result_calls:  outcome == "ok" and result_count == 0   # CHANGED (was: result_count == 0)
```

`ingest`: `outcome = record.get("outcome", "ok")`; `is_error = outcome == "error"` (D5).

### 1.4 Text report (`format_metrics_text`)

Add an `errors` column:

```
Tool calls: 4 (1 error)
  tool                      calls  errors    avg ms    max ms  zero-result
  search_logs                   4       1      10.6      26.1            1
```

Same code path serves both the MCP tool (JSON) and the CLI, so `cli.py` needs no change.

### 1.5 Tests for Part 1

All in `tests/observability/`, run with `python -m pytest tests/observability -q`.

`test_tool_call_log.py`
- `REQUIRED_KEYS` += `"outcome"`; existing envelope test then checks `outcome == "ok"` and
  `"error" not in rec`.
- `test_error_outcome_sets_zero_results_error_text_and_info_level` — `log_tool_call(..., 7,
  outcome="error", error="Error: x")` → `result_count == 0`, `outcome == "error"`,
  `error == "Error: x"`, `level == "INFO"`, message contains `-> error in` and `Error: x`.
- `test_error_text_is_truncated_to_500_chars_and_message_is_single_line` — 1 000-char,
  3-line error → `error` ends with `"..."` and has length 503; `"\n" not in rec["message"]`.
- `test_unknown_outcome_falls_back_to_ok_and_never_raises`.
- `test_toolcall_helper_ok_and_error_return_the_text_and_emit_once` — uses `ToolCall` directly;
  asserts the returned string is the argument and exactly one event per call.
- `test_real_tool_call_site_emits_event` — unchanged expectations plus `outcome == "ok"`.

`test_counters.py`
- `test_error_calls_are_counted_separately_from_zero_results` — 1 ok/3, 1 ok/0, 2 error →
  `count 4, error_calls 2, zero_result_calls 1`, `tool_calls.errors == 2`, durations include all 4.
- `test_replay_treats_records_without_outcome_as_ok` — hand-written JSONL line without
  `outcome` (legacy) and one with `"outcome":"weird"` → both `ok`; `result_count 0` on the
  legacy one still lands in `zero_result_calls`.
- Update `test_replay_skips_malformed_blank_and_unknown_lines` expected `by_tool` dict
  (`+ "error_calls": 0`) and `test_replay_matches_live_counters_for_the_same_events` (add one
  error call; `lines_*` expectations shift by one).
- `test_counters_are_thread_safe` — alternate `outcome` per call; assert
  `sum(error_calls) == n/2` (no lost increments on the new counter).
- `test_format_text_*` — assert the `errors` header and the `(1 error)` suffix.

`test_get_metrics_tool.py`
- `test_get_metrics_reports_error_calls` — one error call → JSON has
  `tool_calls.errors == 1` and `by_tool.x.error_calls == 1`; `zero_result_calls == 0`.

`test_get_metrics_cli.py`
- `events_file` fixture gains one error call; text assertions include the `errors` column;
  `--json` assertion includes `"errors"`.

---

## Part 2 — cover the early-return paths

### 2.1 Mechanical change per tool body

```python
# before
start_time = time.time()
if not project:
    return "Error: project is required."
...
log_tool_call("list_files", {"project": project, "file_type": file_type}, start_time, len(sorted_files))
return "\n".join(output_parts)

# after
call = ToolCall("list_files", {"project": project, "file_type": file_type})
if not project:
    return call.error("Error: project is required.")
...
return call.ok(len(sorted_files), "\n".join(output_parts))
```

Rules applied uniformly:
- `call = ToolCall(...)` replaces `start_time = time.time()` as the first statement; params dict
  is the one the tool already logs today (D6: raw values).
- Every return that starts with `Error` / reports a failure → `call.error(text)`.
- Every other return → `call.ok(n, text)` with the same `n` the tool logs today.
- `_search_docs_sync` keeps `start_time`-equivalent semantics: `index_log_file` also uses
  `elapsed_ms` in its output text — read it from `call.start_time`.

### 2.2 Inventory (from an AST pass over the current source; line numbers as of today)

| Tool body | `error` paths (new) | Reclassified to `error` (emitted as ok/0 today) | Stay `ok` (judgement calls) |
|---|---|---|---|
| `search._search_docs_sync` | 5 — empty query; >1000 chars; query-encoding exception; project not found; no collections | — | "No results found" → `ok/0` |
| `search._search_code_sync` | 0 — pure delegation, no event of its own (allow-listed in guard test) | — | — |
| `search._find_function_sync` | 1 — empty name | — | — |
| `search.search_hex_pattern` | 3 — empty pattern; project not found; no collections | — | "No matches" → `ok/0` |
| `search.find_variable` | 3 — empty name; project not found; no collections | — | "No occurrences" → `ok/0` |
| `documents.get_document` | 2 — invalid path; empty project | 2 — project not found; document not found | — |
| `documents.list_projects` | 0 | — | empty list → `ok/0` |
| `documents.list_files` | 1 — empty project | 2 — project not found; `collection.get` raised | "No files found" → `ok/0` |
| `documents.get_project_summary` | 2 — empty project; not found | — | "indexed but has no chunks" → `ok/0` |
| `documents._compare_projects_sync` | 2 — empty query; missing project name(s) | — | — |
| `management._add_project_sync` | 8 — empty name; empty path; path missing; not a dir; already exists; no sources; indexing failed (rolled back); indexing failed **and** rollback failed | — | re-activated removed project → `ok/0`; 0 chunks indexed → `ok/0` |
| `management._add_file_sync` | 8 — empty file_path; empty project; file missing; not a file; project not found; no index; unreadable; no chunks | — | — |
| `management._add_folder_sync` | 8 — empty folder; empty project; missing; not a dir; project not found; no index; outside base_path; no files matched | — | — |
| `management._add_pattern_sync` | 6 — empty project; empty pattern; bad type; project not found; removed; no index | — | "saved, no files matched yet" and "already exists, no files" → `ok/0` |
| `management.remove_project` | 3 — empty name; not found; config save failed (collection already deleted — still an error to the caller) | — | — |
| `management.clear_project_index` | 3 — empty name; not found; delete raised | — | — |
| `management.remove_file_from_index` | 4 — empty file; empty project; not found; delete raised | — | 0 chunks matched → `ok/0` |
| `logs._search_logs_sync` | 9 — bad severity; no criteria; query >512; pattern >32; bad time filter; start>end; project not found; no collections; every collection failed | — | — |
| `logs._index_log_file_sync` | 7 — empty project; not found; removed; no log sources; bad time_from; bad time_to; no matching files | — | cancelled / per-file errors → `ok/<chunks stored>` (D1) |
| **Total** | **75** | **4** | |

### 2.3 Tests for Part 2

New file `tests/observability/test_tool_call_outcomes.py` (no ChromaDB, no model — `MagicMock`
contexts exactly like the existing `test_real_tool_call_site_emits_event`).

1. **One parametrized table, one row per path in the inventory (79 rows).** Each row:
   `(module, function, kwargs, ctx_factory, expected_text_substring)`. Shared helpers build the
   context:
   - `ctx_no_projects()` — `config.projects == []`, `store.list_collections() == []`,
     `store.get_collection() is None` → covers "not found" / "no collections".
   - `ctx_with_project(name, base_path)` — one `ProjectConfig`, one mocked collection whose
     `get`/`query`/`count` can be given a `side_effect` → covers "raised" paths
     (`list_files`, `clear_project_index`, `remove_file_from_index`, `search_logs` all-failed).
   - `tmp_path` for file-system guards (`add_file` missing/not-a-file, `add_folder` outside
     base_path, `add_project` not-a-dir, `index_log_file` no matching files).
   - `add_project` indexing failure: patch `rag_mcp.indexing_pipeline.IndexingPipeline.index_project`
     with `side_effect=RuntimeError`; the "rollback also failed" row adds
     `loader.save.side_effect=[None, OSError("ro")]`.
   - `search_docs` encoding failure: `ctx.embedding_gen.encode_query.side_effect = RuntimeError`.

   Assertions per row: the tool returns a `str` containing `expected_text_substring`; the events
   **filtered by `tool == <function's tool name>`** (not `len == 1`, because of the documented
   nesting) contain exactly one record with `outcome == "error"`, `result_count == 0`,
   `error == returned text[:500]`, `params` equal to the documented dict with the raw inputs.

2. **Reclassification rows** (4): `get_document` ×2, `list_files` ×2 → now `outcome == "error"`,
   and `zero_result_calls` for that tool stays `0` in `get_metrics_snapshot()`.

3. **"Stay ok" rows** (the 11 judgement calls in the inventory's last column): e.g. `add_pattern` no-files-matched, `remove_file_from_index` 0 chunks,
   `search_docs` no results → `outcome == "ok"`, `result_count == 0`, no `error` key.

4. **Completeness guard** — `test_every_return_in_a_tool_body_goes_through_toolcall`: parse the
   four tool modules with `ast`; for every top-level function that is registered in `register()`
   or is a `_*_sync` body, walk its `Return` nodes (skipping nested `FunctionDef`/`ClassDef`, e.g.
   `_parse_time`, `SilentConsole`) and assert each returns `call.ok(...)` or `call.error(...)`.
   Allow-list: the async wrappers (`return await anyio.to_thread.run_sync(...)`),
   `_search_code_sync`, `metrics.get_metrics`. This is what stops the gap from reopening when a
   new early return is added.

5. **Existing suites as regression net**: `tests/mcp_tools/test_casepack_validation.py` already
   drives most of these returns through the real server context (casepacks in
   `tests/cases/mcp_tools.validation.json`). No casepack changes needed; they must still pass,
   which proves the returned texts are byte-for-byte unchanged. (Note: that package needs the
   `mcp`/`pydantic` pin from `requirements.txt`; it did not import in the sandbox used to write
   this plan — run it in the project venv.)

---

## Part 3 — docs

### 3.1 `doc/OBSERVABILITY.md`

- `tool_call` field table: add `outcome` and `error` rows; replace the **Coverage note** with:
  "Every registered tool emits exactly one `tool_call` per invocation (plus nested `search_docs`
  events for `find_function` / `search_code` / `compare_projects`); validation and lookup failures
  emit `outcome: "error"` with the returned text in `error`." Add an error example line.
- Note the `top_k` raw-vs-clamped change (D6) and that `level` stays `INFO` (D2) with the
  `jq 'select(.outcome=="error")'` filter.
- Counters table: add `tool_calls.errors`, `by_tool.<tool>.error_calls`; rewrite the
  `zero_result_calls` row ("ok calls with `result_count == 0` — errors are **not** counted here").
- MCP tool JSON example and CLI text report: add `errors` / `error_calls` / the new column.
- Replay rules: "records without `outcome` (written before vX.Y) replay as `ok`".
- Recipes: add `# Failed calls, by tool` — `jq -c 'select(.event=="tool_call" and .outcome=="error") | {ts,tool,error}'`
  and `| jq -r .tool | sort | uniq -c`.
- Privacy section: `error` contains the same project/file names already present in `params`;
  nothing new is persisted.

### 3.2 `doc/CLI_REFERENCE.md`, `doc/TOOLS_GUIDE.md`, module docstring

- `CLI_REFERENCE.md` `## get_metrics`: update the sample text report (new column, `(1 error)`).
- `TOOLS_GUIDE.md` §20 `get_metrics`: "tool calls per tool (count, **error calls**, average/max
  duration, zero-result calls)".
- `observability.py` module docstring: add `outcome` / `error` to the `tool_call` block.
- `tools/metrics.py` `get_metrics` docstring (it is the tool description shown to the client).

### 3.3 Bundled copy

`python scripts/sync_package_data.py` after editing — `test_bundled_copy_is_in_sync_with_doc`
fails otherwise.

### 3.4 Tests for Part 3 (`tests/observability/test_docs.py`)

- `test_observability_doc_covers` parametrize list += `'"outcome":"error"'`, `"error_calls"`,
  `"`errors`"`, and `-"Coverage note"` (assert the old paragraph is gone:
  `"no success/error outcome field yet" not in doc`).
- `test_documented_field_lists_match_the_emitted_records`: emit an **error** record too so the
  union of emitted fields includes `outcome` and `error` → both must appear as `` `field` `` in the doc.
- `test_cli_reference_documents_get_metrics_and_env_var`: `+ "errors"` column header present.
- `test_tools_guide_documents_get_metrics_tool`: `+ "error calls"`.

---

## Part 4 (optional) — exceptions that escape a tool body

Today an uncaught exception (e.g. `collection.get` in `get_document`, `ensure_model_loaded()` in
`add_file`) becomes an MCP protocol error and emits nothing — a gap of the same kind, just
rarer. Proposed, separable from Parts 1–3:

- `helpers.guard_tool(tool_name)` decorator applied to each sync body in `register()`: on
  exception, emit `outcome="error"`, `error=f"{type(e).__name__}: {e}"` using the bound
  arguments as `params` (`inspect.signature(fn).bind`), then re-raise unchanged. No
  double-emission risk: `ToolCall.ok/error` run only on `return` paths, an exception means none
  was reached.
- Tests: one row per tool with a `side_effect=RuntimeError` on the first store call →
  `pytest.raises(RuntimeError)` **and** one `outcome == "error"` event whose `error` starts with
  `RuntimeError:`; plus `test_guard_does_not_emit_when_body_already_did`.
- Docs: one sentence in the `tool_call` section ("an exception escaping a tool is also logged as
  `outcome: "error"`, `error` = `ExceptionType: message`").

If you want Parts 1–3 only, nothing above depends on Part 4.

---

## Order of work and verification

1. Part 1 (`observability.py`, `helpers.py`) + its tests → `python -m pytest tests/observability -q`
   (baseline today: 95 passed; expect ≈ 95 + ~10 new, 0 failures).
2. Part 2, one module at a time (`documents.py` → `search.py` → `management.py` → `logs.py`),
   adding the inventory rows as each module is converted; the guard test is written first and is
   expected to fail until the last module is done.
3. Part 3 docs, `scripts/sync_package_data.py`, doc tests.
4. Full run: `python -m pytest tests -q` (including `tests/mcp_tools` and `tests/packaging`) in
   the project venv.
5. Manual smoke: start the server, call `search_docs` with an unknown project, then
   `get_metrics` → `tool_calls.errors == 1`; `rag-mcp get_metrics` on the same file shows the
   `errors` column with `1`.

## Compatibility summary

- **Event schema**: additive (`outcome` always, `error` on failures). Existing `jq` recipes keep working.
- **Counter JSON**: additive (`errors`, `error_calls`); `zero_result_calls` semantics narrowed (D4).
- **Replay**: old files work (D5).
- **Tool return texts**: unchanged byte-for-byte (casepack suite enforces).
- **Behavioural change to be aware of**: `params.top_k` logged as received, not clamped (D6).
