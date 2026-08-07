# JSON Test Case Authoring Guide (pow-mcp-rag)

## Purpose
Add or update tests by editing JSON only.  
Python test code stays stable as runners/adapters.

## Where to add test cases
- Case files: `tests/cases/*.json`
- Schemas: `tests/cases/schema/*.schema.json`
- Generic runners: `tests/runners/*.py` (maintained, not edited per new case)

## Case file conventions
1. One file per domain:
   - `mcp_tools.validation.json`
   - `mcp_tools.execution.json`
   - `server.startup.json`
   - `packaging.paths.json`
2. Keep case IDs unique and stable.
3. Use predictable keys so schema validation can catch mistakes early.

## Base JSON shape
```json
{
  "id": "remove-project-empty-name",
  "runner": "validation_runner",
  "operation": "management.remove_project",
  "inputs": { "name": "" },
  "expect": {
    "result_contains": ["Error", "required"]
  }
}
```

## Supported runner patterns
1. `validation_runner`
   - For input validation, error messages, and simple success checks.
2. `state_runner`
   - For mock call counts, config/state mutation checks.
3. `subprocess_runner`
   - For startup/CLI/subprocess behavior (env, exit code, output checks).

## How to add a new test
1. Pick the correct case pack JSON file.
2. Add a new case object with:
   - `id`
   - `runner`
   - `operation`
   - `inputs`
   - `expect`
3. Validate schema:
   - `python tests/runners/validate_casepacks.py`
4. Run targeted suite:
   - `pytest -q tests/mcp_tools` (or relevant folder)
5. Run full suite:
   - `pytest`

## Using Graphify to create better test cases
Use Graphify to discover impact areas and avoid missing linked behaviors.

Examples:
- `graph_stats(project_path="D:\\GitHub\\pow-mcp-rag-new")`
- `query_graph(project_path="D:\\GitHub\\pow-mcp-rag-new", question="remove_project validation and config save paths", depth=2)`
- `god_nodes(project_path="D:\\GitHub\\pow-mcp-rag-new", top_n=10)`

What to extract:
- Related modules/functions to include in assertions.
- Neighboring test files that should receive parallel cases.
- Shared fixtures used across multiple suites.

## Using RAG MCP to create and execute the plan
Use RAG to fetch exact behavior/fixtures/docs before writing JSON cases.

Examples:
- `search_code(project="pow-mcp-rag-new", query="tests/mcp_tools conftest isolated_server_context", top_k=5)`
- `get_document(project="pow-mcp-rag-new", file_path="tests/server/conftest.py")`
- `search_specs(project="pow-mcp-rag-new", query="test organization and validation strategy", top_k=5)`

Use this to:
- copy real fixture setup patterns into case assumptions,
- align expected messages with actual tool outputs,
- avoid drift between docs and tests.

## Important constraints
- Adding test cases should not require Python edits.
- Python changes are only needed when:
  - introducing a new `runner` type, or
  - introducing a new operation key not in the registry.
- Keep CI behavior unchanged while migrating.

## CI checklist
- Case packs validate with no schema errors.
- Targeted tests pass for affected domain.
- Full `pytest` passes.
- No dependency on repo-local `config/config.yaml` for subprocess tests.
