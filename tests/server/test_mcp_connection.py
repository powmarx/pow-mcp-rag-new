"""
MCP server connection tests.

Verifies that the MCP server starts up correctly, responds to the protocol
handshake, and exposes the expected tools. Uses the official MCP client SDK
to communicate with the server via stdio transport.

The server is started ONCE and all checks run within a single session to avoid
the ~14s model loading cost per subprocess.
"""

import sys
import time
import uuid
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).parent.parent.parent
assert (SCRIPT_DIR / "pyproject.toml").exists(), (
    f"SCRIPT_DIR did not resolve to the repo root: {SCRIPT_DIR}"
)
SERVER_SCRIPT = SCRIPT_DIR / "server.py"
PYTHON = sys.executable

# Add src/ to path
sys.path.insert(0, str(SCRIPT_DIR / "src"))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_mcp_server_full_integration(tmp_path):
    """Full MCP server integration test using a single server process.

    Starts one server subprocess and verifies: startup, handshake, tool listing,
    schemas, and all core tool calls (list_projects, list_files, search_docs,
    error handling). This avoids spawning 10 separate processes (~14s each).
    """
    server_params = StdioServerParameters(
        command=PYTHON,
        args=[str(SERVER_SCRIPT), "--no-reindex"],
        cwd=str(SCRIPT_DIR),
    )

    # Build a unique throwaway project name to avoid collisions with real projects.
    throwaway_project = f"test-integration-{uuid.uuid4().hex[:8]}"

    # Create a minimal file inside tmp_path so the project has something to index.
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("Hello from the throwaway integration-test project.\n")

    start = time.time()
    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init_result = await session.initialize()
            startup_time = time.time() - start

            # --- 1. Server starts and initializes ---
            assert startup_time < 60, f"Server took {startup_time:.1f}s to initialize (limit: 60s)"

            # --- 2. Server info ---
            assert init_result.serverInfo is not None, "Missing serverInfo"
            assert init_result.serverInfo.name, "Empty server name"

            # --- 3. Tools list ---
            tools_result = await session.list_tools()
            tool_names = [t.name for t in tools_result.tools]

            expected_tools = ["search_docs", "get_document", "list_projects", "list_files",
                              "search_hex_pattern", "find_variable", "search_specs", "search_code",
                              "find_function", "compare_projects", "get_project_summary",
                              "add_file", "add_folder", "add_project"]
            missing = [t for t in expected_tools if t not in tool_names]
            assert not missing, f"Missing tools: {missing}"
            assert len(tool_names) >= 14, f"Expected at least 14 tools, got {len(tool_names)}"

            # --- 4. Tool schemas ---
            for tool in tools_result.tools:
                assert tool.inputSchema is not None, f"Tool {tool.name} has no input schema"
                assert "type" in tool.inputSchema, f"Tool {tool.name} schema missing 'type'"

            # --- 5. Call list_projects ---
            result = await session.call_tool("list_projects", {})
            assert result.content is not None, "list_projects: No content"
            assert len(result.content) > 0, "list_projects: Empty content"
            text = result.content[0].text
            assert len(text) > 0, "list_projects: Empty text"
            assert not result.isError, f"list_projects error: {text}"

            # --- Set up throwaway project via the MCP server's add_project tool ---
            # (Must go through call_tool because this test talks to the server over
            #  stdio transport; _add_project_sync is not accessible in-process here.)
            add_result = await session.call_tool("add_project", {
                "name": throwaway_project,
                "path": str(tmp_path),
            })
            add_text = add_result.content[0].text
            assert not add_result.isError, f"add_project failed: {add_text}"

            try:
                # --- 6. Call list_files (against the throwaway project) ---
                result = await session.call_tool("list_files", {
                    "project": throwaway_project,
                })
                text = result.content[0].text
                assert not result.isError, f"list_files error: {text}"
                assert "Total:" in text, "list_files: missing Total count"

                # --- 7. Call search_docs (triggers model, already pre-loaded) ---
                result = await session.call_tool("search_docs", {
                    "query": "dispense rejection",
                    "top_k": 3,
                })
                text = result.content[0].text
                assert not result.isError, f"search_docs error: {text}"
                assert len(text) > 0, "search_docs: Empty result"
                assert "Result" in text, "search_docs: No results found"

                # --- 8. search_docs empty query validation ---
                result = await session.call_tool("search_docs", {"query": "   "})
                text = result.content[0].text
                assert "Error" in text or "error" in text, (
                    f"Expected error for empty query, got: {text[:100]}"
                )

                # --- 9. get_document path traversal validation ---
                # The path-traversal guard fires before any project look-up, so the
                # project name does not affect whether "Error" appears in the response.
                result = await session.call_tool("get_document", {
                    "file_path": "../../../etc/passwd",
                    "project": throwaway_project,
                })
                text = result.content[0].text
                assert "Error" in text, f"Expected error for path traversal, got: {text[:100]}"

            finally:
                # Teardown: remove the throwaway project regardless of pass/fail.
                await session.call_tool("remove_project", {"name": throwaway_project})
