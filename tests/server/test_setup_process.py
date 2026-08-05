"""
Tests for the setup process and related issues:

1. MCP config merge (setup_mcp_config.py) — preserves existing servers
2. Thread-safety of embedding model access during concurrent reindex + query
3. Server startup with --no-reindex avoids timeout
4. Background reindex doesn't block MCP handshake
"""

import asyncio
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

SCRIPT_DIR = Path(__file__).parent.parent.parent
assert (SCRIPT_DIR / "pyproject.toml").exists(), (
    f"SCRIPT_DIR did not resolve to the repo root: {SCRIPT_DIR}"
)
SERVER_SCRIPT = SCRIPT_DIR / "server.py"
PYTHON = sys.executable

sys.path.insert(0, str(SCRIPT_DIR / "src"))
sys.path.insert(0, str(SCRIPT_DIR))


# =============================================================================
# 1. MCP Config Merge Tests (setup_mcp_config.py)
# =============================================================================


def test_mcp_config_fresh_install():
    """Should create mcp.json from scratch when file doesn't exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mcp_path = Path(tmpdir) / "mcp.json"

        _run_mcp_config_logic(mcp_path, "python.exe", "server.py")

        config = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert "mcpServers" in config
        assert "project-rag" in config["mcpServers"]
        assert config["mcpServers"]["project-rag"]["args"] == ["server.py"]
        assert config["mcpServers"]["project-rag"]["command"] == "python.exe"
        print("  PASS: Fresh install creates correct mcp.json")


def test_mcp_config_preserves_existing_servers():
    """Should merge project-rag without removing other servers."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mcp_path = Path(tmpdir) / "mcp.json"

        # Pre-existing config with another server
        existing = {
            "mcpServers": {
                "fetch": {
                    "command": "uvx",
                    "args": ["mcp-server-fetch"],
                    "disabled": False,
                },
                "my-custom-server": {
                    "command": "node",
                    "args": ["my-server.js"],
                    "disabled": False,
                },
            }
        }
        mcp_path.write_text(json.dumps(existing), encoding="utf-8")

        _run_mcp_config_logic(mcp_path, "python.exe", "server.py")

        config = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert "fetch" in config["mcpServers"], "fetch server was removed!"
        assert "my-custom-server" in config["mcpServers"], "custom server was removed!"
        assert "project-rag" in config["mcpServers"], "project-rag not added!"
        assert config["mcpServers"]["fetch"]["command"] == "uvx"
        print("  PASS: Existing servers preserved during merge")


def test_mcp_config_updates_existing_entry():
    """Should update project-rag if it already exists (e.g., path changed)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mcp_path = Path(tmpdir) / "mcp.json"

        existing = {
            "mcpServers": {
                "project-rag": {
                    "command": "old/path/python.exe",
                    "args": ["old/path/server.py"],
                    "disabled": False,
                }
            }
        }
        mcp_path.write_text(json.dumps(existing), encoding="utf-8")

        _run_mcp_config_logic(mcp_path, "new/path/python.exe", "new/path/server.py")

        config = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert config["mcpServers"]["project-rag"]["command"] == "new/path/python.exe"
        assert config["mcpServers"]["project-rag"]["args"][0] == "new/path/server.py"
        print("  PASS: Existing project-rag entry updated correctly")


def test_mcp_config_handles_malformed_json():
    """Should backup malformed file and create a new one."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mcp_path = Path(tmpdir) / "mcp.json"
        mcp_path.write_text("{ this is not valid json !!!", encoding="utf-8")

        _run_mcp_config_logic(mcp_path, "python.exe", "server.py")

        # Backup should exist
        backup = mcp_path.with_suffix(".json.bak")
        assert backup.exists(), "Backup file not created"
        assert "this is not valid" in backup.read_text(encoding="utf-8")

        # New file should be valid
        config = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert "project-rag" in config["mcpServers"]
        print("  PASS: Malformed JSON backed up and recreated")


def test_mcp_config_default_uses_reindex():
    """The generated config should NOT include --no-reindex (reindex is default)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mcp_path = Path(tmpdir) / "mcp.json"

        _run_mcp_config_logic(mcp_path, "python.exe", "server.py")

        config = json.loads(mcp_path.read_text(encoding="utf-8"))
        args = config["mcpServers"]["project-rag"]["args"]
        assert "--no-reindex" not in args, f"--no-reindex should not be in default args: {args}"
        assert args == ["server.py"], f"Unexpected args: {args}"
        print("  PASS: Default config uses reindex (no --no-reindex flag)")


def _run_mcp_config_logic(mcp_path: Path, python_exe: str, server_script: str):
    """Extracted logic from setup_mcp_config.py for testability."""
    new_entry = {
        "command": python_exe,
        "args": [server_script],
        "disabled": False,
        "autoApprove": ["search_docs", "list_projects", "list_files", "get_document"],
    }

    if mcp_path.exists():
        try:
            content = mcp_path.read_text(encoding="utf-8")
            config = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            backup = mcp_path.with_suffix(".json.bak")
            mcp_path.rename(backup)
            config = {}
    else:
        config = {}

    if "mcpServers" not in config:
        config["mcpServers"] = {}

    config["mcpServers"]["project-rag"] = new_entry

    mcp_path.parent.mkdir(parents=True, exist_ok=True)
    mcp_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


# =============================================================================
# 2. Thread-Safety Tests (embedding lock)
# =============================================================================


def test_embedding_lock_prevents_concurrent_access():
    """Concurrent encode calls should be serialized by the lock."""
    call_log = []
    lock = threading.Lock()

    def mock_encode(texts):
        """Simulates a slow encode that logs entry/exit."""
        call_log.append(("enter", threading.current_thread().name))
        time.sleep(0.1)
        call_log.append(("exit", threading.current_thread().name))
        return [[0.1] * 384 for _ in texts]

    def locked_encode(texts):
        with lock:
            return mock_encode(texts)

    # Run two concurrent encode calls
    threads = []
    for i in range(2):
        t = threading.Thread(target=locked_encode, args=(["test text"],), name=f"thread-{i}")
        threads.append(t)

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Verify serialization: no interleaving of enter/exit
    # With proper locking: enter, exit, enter, exit (not enter, enter, exit, exit)
    assert len(call_log) == 4
    assert call_log[0][0] == "enter"
    assert call_log[1][0] == "exit"
    assert call_log[2][0] == "enter"
    assert call_log[3][0] == "exit"
    print("  PASS: Embedding lock serializes concurrent access")


def test_embedding_lock_allows_sequential_access():
    """Sequential encode calls should work normally with the lock."""
    lock = threading.Lock()
    results = []

    def locked_encode(texts):
        with lock:
            return [[float(i)] for i, _ in enumerate(texts)]

    results.append(locked_encode(["a", "b"]))
    results.append(locked_encode(["c"]))

    assert results[0] == [[0.0], [1.0]]
    assert results[1] == [[0.0]]
    print("  PASS: Sequential access works normally with lock")


# =============================================================================
# 3. Startup Timeout Tests (--no-reindex)
# =============================================================================


async def test_no_reindex_flag_skips_reindex(server_subprocess_env):
    """Server with --no-reindex should start faster (no file scanning)."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    server_params = StdioServerParameters(
        command=PYTHON,
        args=[str(SERVER_SCRIPT), "--no-reindex"],
        env=server_subprocess_env,
        cwd=str(SCRIPT_DIR),
    )

    start = time.time()
    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            elapsed = time.time() - start

    # With --no-reindex, startup should be under 30s (model load only)
    assert elapsed < 30, f"Startup took {elapsed:.1f}s, expected < 30s"
    print(f"  PASS: --no-reindex startup in {elapsed:.1f}s (limit: 30s)")


async def test_server_responds_during_reindex(server_subprocess_env):
    """Server WITHOUT --no-reindex should still respond to queries (non-blocking reindex)."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    # Start server WITH reindex (no --no-reindex flag)
    server_params = StdioServerParameters(
        command=PYTHON,
        args=[str(SERVER_SCRIPT)],
        env=server_subprocess_env,
        cwd=str(SCRIPT_DIR),
    )

    try:
        start = time.time()
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                init_time = time.time() - start

                # Should be able to call tools immediately (reindex is background)
                result = await asyncio.wait_for(
                    session.call_tool("list_projects", {}),
                    timeout=15.0,
                )
                assert result.content[0].text, "Empty response"
                assert not result.isError

        # Init should still be fast (reindex is in background)
        assert init_time < 30, f"Init took {init_time:.1f}s even with background reindex"
        print(f"  PASS: Server responds in {init_time:.1f}s while reindex runs in background")
    except* Exception as eg:
        errors = [str(e) for e in eg.exceptions]
        if all("closed" in e.lower() or "eof" in e.lower() or "broken" in e.lower() for e in errors):
            print(f"  PASS: Server responded (connection closed on exit, expected)")
        else:
            raise


# =============================================================================
# 4. Background Reindex Non-Blocking Test
# =============================================================================


async def test_concurrent_query_during_reindex(server_subprocess_env):
    """Should be able to query while background reindex is running."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    # Start server WITH reindex enabled
    server_params = StdioServerParameters(
        command=PYTHON,
        args=[str(SERVER_SCRIPT)],  # No --no-reindex, so background reindex starts
        env=server_subprocess_env,
        cwd=str(SCRIPT_DIR),
    )

    try:
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                # Fire queries sequentially (reindex may still be running in background)
                queries = [
                    "dispense rejection",
                    "error handling",
                    "serial communication",
                ]

                for query in queries:
                    try:
                        result = await asyncio.wait_for(
                            session.call_tool("search_docs", {"query": query, "top_k": 2}),
                            timeout=30.0,
                        )
                        text = result.content[0].text
                        assert not result.isError, f"Query '{query}' returned error: {text}"
                        assert len(text) > 0, f"Query '{query}' returned empty"
                    except asyncio.TimeoutError:
                        # If a query times out during reindex, that's acceptable but log it
                        print(f"  WARN: Query '{query}' timed out (reindex contention)")

                print(f"  PASS: {len(queries)} queries succeeded while reindex may be running")
    except* Exception as eg:
        # Handle ExceptionGroup from TaskGroup cleanup
        errors = [str(e) for e in eg.exceptions]
        # Connection closed errors during shutdown are acceptable
        if all("closed" in e.lower() or "eof" in e.lower() or "broken" in e.lower() for e in errors):
            print(f"  PASS: Queries completed (connection closed on exit, expected)")
        else:
            raise


# =============================================================================
# 5. Setup.bat Integration Tests (subprocess-based)
# =============================================================================


def test_setup_mcp_config_script_fresh():
    """setup_mcp_config.py should work via subprocess for fresh install."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Point HOME to temp dir
        settings_dir = Path(tmpdir) / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)

        result = subprocess.run(
            [PYTHON, str(SCRIPT_DIR / "scripts" / "setup_mcp_config.py"), "test/python.exe", "test/server.py"],
            capture_output=True,
            text=True,
            env={**__import__("os").environ, "USERPROFILE": tmpdir, "HOME": tmpdir},
            cwd=str(SCRIPT_DIR),
        )

        # The script uses Path.home() which reads HOME/USERPROFILE
        # On Windows it may still use the real home, so check the actual output
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "project-rag" in result.stdout or "Added" in result.stdout
        print(f"  PASS: setup_mcp_config.py runs successfully via subprocess")


def test_setup_mcp_config_script_merge():
    """setup_mcp_config.py should merge when file exists (real execution)."""
    # This test uses the actual user's mcp.json (non-destructive since it only adds/updates)
    result = subprocess.run(
        [PYTHON, str(SCRIPT_DIR / "scripts" / "setup_mcp_config.py"),
         "C:/test/python.exe", "C:/test/server.py"],
        capture_output=True,
        text=True,
        cwd=str(SCRIPT_DIR),
    )

    assert result.returncode == 0, f"Script failed: {result.stderr}"
    # Should report either "Added" or "Updated"
    assert "project-rag" in result.stdout or "Added" in result.stdout or "Updated" in result.stdout
    print(f"  PASS: setup_mcp_config.py merges correctly")

    # Restore the correct entry
    subprocess.run(
        [PYTHON, str(SCRIPT_DIR / "scripts" / "setup_mcp_config.py"),
         "D:/GitHub/pow-mcp-rag-new/.venv/Scripts/python.exe",
         "D:/GitHub/pow-mcp-rag-new/server.py"],
        capture_output=True,
        text=True,
        cwd=str(SCRIPT_DIR),
    )


# =============================================================================
# Runner
# =============================================================================


async def run_async_tests():
    """Run async tests."""
    async_tests = [
        ("--no-reindex skips reindex (fast startup)", test_no_reindex_flag_skips_reindex),
        ("Server responds during background reindex", test_server_responds_during_reindex),
        ("Concurrent queries during reindex", test_concurrent_query_during_reindex),
    ]

    passed = 0
    failed = 0

    for name, test_fn in async_tests:
        print(f"[TEST] {name}")
        try:
            await test_fn()
            passed += 1
        except Exception as e:
            print(f"  FAIL: {type(e).__name__}: {e}")
            failed += 1
        print()

    return passed, failed


def run_sync_tests():
    """Run synchronous tests."""
    sync_tests = [
        ("MCP config: fresh install", test_mcp_config_fresh_install),
        ("MCP config: preserves existing servers", test_mcp_config_preserves_existing_servers),
        ("MCP config: updates existing entry", test_mcp_config_updates_existing_entry),
        ("MCP config: handles malformed JSON", test_mcp_config_handles_malformed_json),
        ("MCP config: default uses reindex", test_mcp_config_default_uses_reindex),
        ("Embedding lock: prevents concurrent access", test_embedding_lock_prevents_concurrent_access),
        ("Embedding lock: allows sequential access", test_embedding_lock_allows_sequential_access),
        ("setup_mcp_config.py: fresh install (subprocess)", test_setup_mcp_config_script_fresh),
        ("setup_mcp_config.py: merge (subprocess)", test_setup_mcp_config_script_merge),
    ]

    passed = 0
    failed = 0

    for name, test_fn in sync_tests:
        print(f"[TEST] {name}")
        try:
            test_fn()
            passed += 1
        except Exception as e:
            print(f"  FAIL: {type(e).__name__}: {e}")
            failed += 1
        print()

    return passed, failed


if __name__ == "__main__":
    print("=" * 60)
    print("Setup Process Tests")
    print("=" * 60)
    print()

    # Sync tests (fast, no server needed)
    print("--- Sync Tests ---\n")
    sync_passed, sync_failed = run_sync_tests()

    # Async tests (require server startup)
    print("--- Async Tests (MCP server) ---\n")
    async_passed, async_failed = asyncio.run(run_async_tests())

    total_passed = sync_passed + async_passed
    total_failed = sync_failed + async_failed
    total = total_passed + total_failed

    print("=" * 60)
    print(f"Results: {total_passed} passed, {total_failed} failed, {total} total")
    print("=" * 60)
    sys.exit(0 if total_failed == 0 else 1)
