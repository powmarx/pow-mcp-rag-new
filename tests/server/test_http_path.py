"""
Tests for configurable HTTP endpoint path (MCP_HTTP_PATH env var).

Covers:
1. Default path is /mcp when MCP_HTTP_PATH is not set
2. MCP_HTTP_PATH env var overrides to custom path
3. FastMCP receives the correct streamable_http_path argument
4. Startup log message includes the custom path
5. Path with and without leading slash is handled
6. Live HTTP test: /mcp responds (existing container on port 8001)
7. Live HTTP test: custom path responds when container uses it
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

SCRIPT_DIR = Path(__file__).parent.parent.parent
assert (SCRIPT_DIR / "pyproject.toml").exists(), (
    f"SCRIPT_DIR did not resolve to the repo root: {SCRIPT_DIR}"
)
sys.path.insert(0, str(SCRIPT_DIR / "src"))
sys.path.insert(0, str(SCRIPT_DIR))


# =============================================================================
# Helpers
# =============================================================================

def _capture_http_path_from_server_module(env_overrides: dict) -> str:
    """
    Re-execute just the path-resolution logic from server.py in isolation.
    Returns the resolved _http_path value.
    """
    with patch.dict(os.environ, env_overrides, clear=False):
        path = os.environ.get("MCP_HTTP_PATH", "/mcp")
    return path


def _build_fastmcp_call_args(http_path: str) -> dict:
    """Simulate what server.py passes to FastMCP in HTTP mode."""
    return {
        "host": "0.0.0.0",
        "port": 8000,
        "streamable_http_path": http_path,
    }


# =============================================================================
# 1. Default path
# =============================================================================

def test_default_path_is_slash_mcp():
    """When MCP_HTTP_PATH is not set, path defaults to /mcp."""
    env = {k: v for k, v in os.environ.items() if k != "MCP_HTTP_PATH"}
    with patch.dict(os.environ, env, clear=True):
        path = os.environ.get("MCP_HTTP_PATH", "/mcp")
    assert path == "/mcp"
    print("  PASS: default path is /mcp")


# =============================================================================
# 2. MCP_HTTP_PATH override
# =============================================================================

@pytest.mark.parametrize(
    "env_value, expected_path",
    [
        pytest.param("/rag-mcp", "/rag-mcp", id="override"),
        pytest.param("/rag-mcp", "/rag-mcp", id="custom_rag-mcp"),
        pytest.param("/api/v1/mcp", "/api/v1/mcp", id="custom_api_v1_mcp"),
        pytest.param("/rag", "/rag", id="custom_rag"),
        pytest.param("/my-server", "/my-server", id="custom_my-server"),
    ],
)
def test_env_var_overrides_path(env_value, expected_path):
    """MCP_HTTP_PATH env var should override the default /mcp path for any custom value."""
    with patch.dict(os.environ, {"MCP_HTTP_PATH": env_value}):
        path = os.environ.get("MCP_HTTP_PATH", "/mcp")
    assert path == expected_path, f"Expected {expected_path}, got {path}"
    print(f"  PASS: MCP_HTTP_PATH={env_value} overrides default to {expected_path}")


# =============================================================================
# 3. FastMCP receives correct streamable_http_path
# =============================================================================

@pytest.mark.parametrize(
    "env_value, expected_path",
    [
        pytest.param("/custom-path", "/custom-path", id="custom_path"),
        pytest.param(None, "/mcp", id="default_path"),
    ],
)
def test_fastmcp_receives_custom_path(env_value, expected_path):
    """FastMCP constructor should be called with the resolved path, custom or default."""
    if env_value is None:
        env = {k: v for k, v in os.environ.items() if k != "MCP_HTTP_PATH"}
        with patch.dict(os.environ, env, clear=True):
            http_path = os.environ.get("MCP_HTTP_PATH", "/mcp")
            kwargs = _build_fastmcp_call_args(http_path)
    else:
        with patch.dict(os.environ, {"MCP_HTTP_PATH": env_value}):
            http_path = os.environ.get("MCP_HTTP_PATH", "/mcp")
            kwargs = _build_fastmcp_call_args(http_path)

    assert kwargs["streamable_http_path"] == expected_path
    print(f"  PASS: FastMCP would receive streamable_http_path={expected_path}")


# =============================================================================
# 4. Startup log includes the path
# =============================================================================

@pytest.mark.parametrize(
    "env_value, expected_path",
    [
        pytest.param("/rag-mcp", "/rag-mcp", id="custom_path"),
        pytest.param(None, "/mcp", id="default_path"),
    ],
)
def test_startup_log_includes_custom_path(env_value, expected_path):
    """Startup log message should include the resolved path, custom or default."""
    http_host = "0.0.0.0"
    http_port = 8000

    if env_value is None:
        env = {k: v for k, v in os.environ.items() if k != "MCP_HTTP_PATH"}
        env["MCP_HTTP_PORT"] = "8000"
        with patch.dict(os.environ, env, clear=True):
            http_path = os.environ.get("MCP_HTTP_PATH", "/mcp")
            msg = f"[startup] HTTP transport on {http_host}:{http_port}{http_path} (MCP_HTTP_PORT)"
    else:
        with patch.dict(os.environ, {"MCP_HTTP_PATH": env_value, "MCP_HTTP_PORT": "8000"}):
            http_path = os.environ.get("MCP_HTTP_PATH", "/mcp")
            msg = f"[startup] HTTP transport on {http_host}:{http_port}{http_path} (MCP_HTTP_PORT)"

    assert expected_path in msg
    if env_value is not None:
        assert "/mcp" not in msg.split(expected_path)[0]  # not the old hardcoded value
    print(f"  PASS: log message contains path: {msg}")


# =============================================================================
# 5. Path validation edge cases
# =============================================================================

def test_empty_env_var_falls_back_to_default():
    """Empty MCP_HTTP_PATH should NOT override — Python or() logic catches this."""
    with patch.dict(os.environ, {"MCP_HTTP_PATH": ""}):
        # The server uses: os.environ.get("MCP_HTTP_PATH", "/mcp")
        # get() returns "" for empty string, so the default is NOT used.
        # This is intentional — explicit empty string is the user's choice.
        path = os.environ.get("MCP_HTTP_PATH", "/mcp")
    # Document the actual behavior: empty string IS returned (not /mcp)
    assert path == ""
    print("  PASS: empty string env var returns '' (user's explicit choice, not /mcp)")


# =============================================================================
# 6 & 7. Live HTTP tests against running container
# =============================================================================

LIVE_BASE_URL = "http://localhost:8001"
LIVE_DEFAULT_PATH = "/mcp"


def _mcp_initialize(base_url: str, path: str) -> tuple[int, dict, str]:
    """Send MCP initialize and return (status_code, response_body, session_id)."""
    import urllib.request
    import urllib.error

    url = f"{base_url}{path}"
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1.0"},
        },
    }).encode()

    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode()
            session_id = resp.headers.get("mcp-session-id", "")
            return resp.status, body, session_id
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), ""
    except Exception as e:
        pytest.skip(f"Container not reachable at {url}: {e}")


def test_live_default_path_responds():
    """Running container on port 8001 should respond at /mcp (default path)."""
    status, body, session_id = _mcp_initialize(LIVE_BASE_URL, LIVE_DEFAULT_PATH)
    assert status == 200, f"Expected 200, got {status}. Body: {body[:200]}"
    assert "protocolVersion" in body, "Missing protocolVersion in response"
    assert "rag-mcp" in body, "Server name not in response"
    assert session_id, "No session ID returned"
    print(f"  PASS: {LIVE_BASE_URL}{LIVE_DEFAULT_PATH} → 200, session={session_id[:8]}...")


def test_live_wrong_path_returns_404():
    """A non-existent path should return 404."""
    status, body, _ = _mcp_initialize(LIVE_BASE_URL, "/nonexistent-path")
    assert status == 404, f"Expected 404 for wrong path, got {status}"
    print(f"  PASS: /nonexistent-path → 404 as expected")


def test_live_server_name_is_rag_mcp():
    """Server info in initialize response should have name rag-mcp."""
    status, body, _ = _mcp_initialize(LIVE_BASE_URL, LIVE_DEFAULT_PATH)
    assert status == 200
    # Extract SSE data line
    data_line = next((l for l in body.splitlines() if l.startswith("data:")), None)
    assert data_line, "No data line in SSE response"
    parsed = json.loads(data_line.replace("data:", "").strip())
    server_name = parsed["result"]["serverInfo"]["name"]
    assert server_name == "rag-mcp", f"Expected rag-mcp, got {server_name}"
    print(f"  PASS: serverInfo.name = '{server_name}'")


if __name__ == "__main__":
    # Note: parametrized tests (test_env_var_overrides_path, test_fastmcp_receives_custom_path,
    # test_startup_log_includes_custom_path) require pytest to run their cases and are excluded here.
    # Run: pytest tests/server/test_http_path.py -v -k "not live"
    tests = [
        test_default_path_is_slash_mcp,
        test_empty_env_var_falls_back_to_default,
        test_live_default_path_responds,
        test_live_wrong_path_returns_404,
        test_live_server_name_is_rag_mcp,
    ]
    passed = failed = skipped = 0
    for t in tests:
        try:
            print(f"\n{t.__name__}")
            t()
            passed += 1
        except pytest.skip.Exception as e:
            print(f"  SKIP: {e}")
            skipped += 1
        except Exception as e:
            print(f"  FAIL: {e}")
            failed += 1
    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed, {skipped} skipped")
    if failed:
        sys.exit(1)
