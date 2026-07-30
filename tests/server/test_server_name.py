"""
Tests for configurable MCP server name feature.

Covers:
1. _server_name() — default reads from server_info.json
2. _server_name(override) — override bypasses server_info.json
3. _write_entry() with custom name — key written correctly to mcp.json
4. Docker mode: --server-name flag uses custom name
5. Docker mode: omitting --server-name uses default from server_info.json
6. Native mode: --server-name flag uses custom name
7. server_info.json default value is rag-mcp
8. Existing other servers are preserved when using custom name
9. Update: second call with same custom name overwrites, doesn't duplicate
"""

import json
import sys
import tempfile
import pytest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).parent.parent.parent
assert (SCRIPT_DIR / "pyproject.toml").exists(), (
    f"SCRIPT_DIR did not resolve to the repo root: {SCRIPT_DIR}"
)
SETUP_MCP_SCRIPT = SCRIPT_DIR / "scripts" / "setup_mcp_config.py"
SERVER_INFO_PATH = SCRIPT_DIR / "config" / "server_info.json"

sys.path.insert(0, str(SCRIPT_DIR / "scripts"))

# Import the module under test
import importlib.util

spec = importlib.util.spec_from_file_location("setup_mcp_config", SETUP_MCP_SCRIPT)
smc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smc)


# =============================================================================
# 1. _server_name() — default
# =============================================================================

def test_server_name_default_reads_from_server_info():
    """Default server name should come from server_info.json."""
    name = smc._server_name()
    assert isinstance(name, str)
    assert len(name) > 0
    print(f"  PASS: default server name = '{name}'")


def test_server_name_default_is_rag_mcp():
    """The default name in server_info.json must be rag-mcp."""
    info = json.loads(SERVER_INFO_PATH.read_text(encoding="utf-8"))
    assert info["name"] == "rag-mcp", (
        f"Expected 'rag-mcp', got '{info['name']}'. "
        "Update server_info.json."
    )
    print("  PASS: server_info.json default is rag-mcp")


# =============================================================================
# 2. _server_name(override)
# =============================================================================

def test_server_name_override_bypasses_file():
    """Passing an override should return that value without reading the file."""
    result = smc._server_name("my-custom-rag")
    assert result == "my-custom-rag"
    print("  PASS: override bypasses server_info.json")


@pytest.mark.parametrize(
    "override",
    [
        pytest.param("", id="empty-string"),
        pytest.param(None, id="none"),
    ],
)
def test_server_name_empty_or_none_override_falls_back_to_file(override):
    """Empty string or None override should fall back to server_info.json."""
    result = smc._server_name(override)
    expected = json.loads(SERVER_INFO_PATH.read_text(encoding="utf-8"))["name"]
    assert result == expected
    print(f"  PASS: override={override!r} falls back to '{result}'")


# =============================================================================
# 3. _write_entry() with custom name
# =============================================================================

def test_write_entry_uses_custom_server_name():
    """_write_entry should use whatever server name is passed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mcp_path = Path(tmpdir) / "mcp.json"
        entry = {"command": "docker", "args": ["run"], "disabled": False, "autoApprove": []}

        smc._write_entry(mcp_path, "my-special-rag", entry)

        config = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert "my-special-rag" in config["mcpServers"]
        assert "rag-mcp" not in config["mcpServers"]
        assert config["mcpServers"]["my-special-rag"]["command"] == "docker"
        print("  PASS: _write_entry writes under custom server name")


def test_write_entry_default_name_is_rag_mcp():
    """When using default name, key in mcp.json should be rag-mcp."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mcp_path = Path(tmpdir) / "mcp.json"
        entry = {"command": "docker", "args": ["run"], "disabled": False, "autoApprove": []}

        smc._write_entry(mcp_path, smc._server_name(), entry)

        config = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert "rag-mcp" in config["mcpServers"]
        print("  PASS: default write uses rag-mcp key")


# =============================================================================
# 4 & 5. Docker mode: --server-name flag
# =============================================================================

def _run_docker_mode(extra_args: list, out_path: Path):
    """Helper: invoke _docker_main() with patched sys.argv and captured output."""
    argv = [
        "setup_mcp_config.py", "--docker",
        "--projects-dir", "C:/Users/test/GIT",
        "--image", "my-rag-image:latest",
        "--data-volume", "rag-mcp-data",
        "--out", str(out_path),
    ] + extra_args

    with patch.object(sys, "argv", argv):
        smc._docker_main()


def test_docker_mode_custom_server_name():
    """Docker mode --server-name should write under the custom key."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mcp_path = Path(tmpdir) / "mcp.json"

        _run_docker_mode(["--server-name", "my-rag"], mcp_path)

        config = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert "my-rag" in config["mcpServers"], "custom name not found"
        assert "rag-mcp" not in config["mcpServers"], "default name should not appear"
        entry = config["mcpServers"]["my-rag"]
        assert entry["command"] == "docker"
        assert "python" in entry["args"]
        assert "server.py" in entry["args"]
        print("  PASS: docker mode --server-name writes under custom key")


def test_docker_mode_default_server_name():
    """Docker mode without --server-name should use rag-mcp."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mcp_path = Path(tmpdir) / "mcp.json"

        _run_docker_mode([], mcp_path)

        config = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert "rag-mcp" in config["mcpServers"], "default name not found"
        print("  PASS: docker mode without --server-name uses rag-mcp")


def test_docker_mode_entry_content():
    """Docker mode entry should contain correct docker run args."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mcp_path = Path(tmpdir) / "mcp.json"

        _run_docker_mode([], mcp_path)

        config = json.loads(mcp_path.read_text(encoding="utf-8"))
        entry = config["mcpServers"]["rag-mcp"]
        args = entry["args"]
        assert "run" in args
        assert "-i" in args
        assert "--rm" in args
        assert "my-rag-image:latest" in args
        assert "--no-reindex" in args
        assert "C:/Users/test/GIT:/projects:ro" in args
        assert "rag-mcp-data:/app/data" in args
        print("  PASS: docker mode entry has correct docker run args")


# =============================================================================
# 6. Native mode: --server-name flag
# =============================================================================

def test_native_mode_custom_server_name():
    """Native mode --server-name should write under the custom key."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mcp_path = Path(tmpdir) / "mcp.json"

        with patch.object(sys, "argv", [
            "setup_mcp_config.py",
            "C:/python.exe",
            "C:/server.py",
            "--server-name", "native-custom-rag",
        ]):
            # Patch home() so it writes to our temp dir
            with patch.object(Path, "home", return_value=Path(tmpdir)):
                # Reconstruct the expected path: home() / ".kiro" / "settings" / "mcp.json"
                (Path(tmpdir) / ".kiro" / "settings").mkdir(parents=True)
                smc.main()

        expected_path = Path(tmpdir) / ".kiro" / "settings" / "mcp.json"
        config = json.loads(expected_path.read_text(encoding="utf-8"))
        assert "native-custom-rag" in config["mcpServers"]
        assert config["mcpServers"]["native-custom-rag"]["command"] == "C:/python.exe"
        print("  PASS: native mode --server-name writes under custom key")


# =============================================================================
# 8. Existing servers preserved with custom name
# =============================================================================

def test_custom_name_preserves_other_servers():
    """Using a custom server name should not remove other servers."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mcp_path = Path(tmpdir) / "mcp.json"

        # Pre-existing config
        existing = {
            "mcpServers": {
                "fetch": {"command": "uvx", "args": ["mcp-server-fetch"], "disabled": False},
                "github": {"command": "npx", "args": ["@github/mcp"], "disabled": False},
            }
        }
        mcp_path.write_text(json.dumps(existing), encoding="utf-8")

        _run_docker_mode(["--server-name", "my-rag"], mcp_path)

        config = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert "fetch" in config["mcpServers"], "fetch server was removed"
        assert "github" in config["mcpServers"], "github server was removed"
        assert "my-rag" in config["mcpServers"], "custom rag server not added"
        assert len(config["mcpServers"]) == 3
        print("  PASS: other servers preserved when using custom name")


# =============================================================================
# 9. Second call overwrites, doesn't duplicate
# =============================================================================

def test_custom_name_second_call_updates_not_duplicates():
    """Calling twice with the same custom name should update, not add a duplicate."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mcp_path = Path(tmpdir) / "mcp.json"

        _run_docker_mode(["--server-name", "my-rag", "--image", "my-rag-image:v1"], mcp_path)
        _run_docker_mode(["--server-name", "my-rag", "--image", "my-rag-image:v2"], mcp_path)

        config = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert list(config["mcpServers"].keys()) == ["my-rag"], "should only have one entry"
        assert "my-rag-image:v2" in config["mcpServers"]["my-rag"]["args"], "should have updated to v2"
        print("  PASS: second call updates existing entry, no duplicate")


# =============================================================================
# Run all tests
# =============================================================================

if __name__ == "__main__":
    tests = [
        test_server_name_default_reads_from_server_info,
        test_server_name_default_is_rag_mcp,
        test_server_name_override_bypasses_file,
        test_server_name_empty_string_override_falls_back_to_file,
        test_server_name_none_override_falls_back_to_file,
        test_write_entry_uses_custom_server_name,
        test_write_entry_default_name_is_rag_mcp,
        test_docker_mode_custom_server_name,
        test_docker_mode_default_server_name,
        test_docker_mode_entry_content,
        test_native_mode_custom_server_name,
        test_custom_name_preserves_other_servers,
        test_custom_name_second_call_updates_not_duplicates,
    ]

    passed = failed = 0
    for t in tests:
        try:
            print(f"\n{t.__name__}")
            t()
            passed += 1
        except Exception as e:
            print(f"  FAIL: {e}")
            failed += 1

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed:
        sys.exit(1)
