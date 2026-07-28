#!/bin/bash
# RAG MCP Server - One-command setup (Linux/macOS)
# Automatically detects sibling git projects and indexes them.

set -e

echo "=== RAG MCP Server Setup ==="
echo ""

# Detect script directory and PROJECTS_ROOT
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECTS_ROOT="$(dirname "$SCRIPT_DIR")"

echo "Detected PROJECTS_ROOT: $PROJECTS_ROOT"
echo ""

# Find Python 3
PYTHON=""
if command -v python3 &> /dev/null; then
    PYTHON="python3"
elif command -v python &> /dev/null; then
    PYTHON="python"
else
    echo "ERROR: Python not found. Install Python 3.10+ and try again."
    exit 1
fi

echo "[1/5] Creating virtual environment..."
"$PYTHON" -m venv "$SCRIPT_DIR/.venv"

# Verify the venv was created correctly (bin/python must exist)
if [ ! -x "$SCRIPT_DIR/.venv/bin/python" ]; then
    echo "ERROR: Virtual environment is incomplete ('.venv/bin/python' not found)."
    echo "       On Debian/Ubuntu install the venv package and retry:"
    echo "         sudo apt install python3-venv"
    echo "       Then remove the broken venv and re-run: rm -rf '$SCRIPT_DIR/.venv' && ./setup.sh"
    exit 1
fi

echo "[2/5] Installing dependencies..."
"$SCRIPT_DIR/.venv/bin/python" -m pip install -r "$SCRIPT_DIR/requirements.txt" --quiet

# Create config.yaml from template if it doesn't exist
if [ ! -f "$SCRIPT_DIR/config/config.yaml" ]; then
    echo "  Creating config.yaml from template..."
    cp "$SCRIPT_DIR/config/config.template.yaml" "$SCRIPT_DIR/config/config.yaml"
fi

echo "[3/5] Discovering root folders (you'll choose which to index)..."
"$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/scripts/setup_discover.py" "$PROJECTS_ROOT"

echo "[4/5] Converting PDFs to Markdown..."
"$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/indexer.py" --convert-pdfs

echo "[5/5] Indexing all projects..."
"$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/indexer.py"

echo ""

# --- Kiro IDE integration (skipped if Kiro not installed) ---
if [ -d "$HOME/.kiro" ]; then
    echo "[bonus] Installing MCP config for Kiro..."
    mkdir -p "$HOME/.kiro/settings"

    # Merge project-rag into existing mcp.json (preserves other servers)
    "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/scripts/setup_mcp_config.py" "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/server.py"

    echo ""
    echo "[bonus] Installing Re-index RAG hook..."
    mkdir -p "$SCRIPT_DIR/.kiro/hooks"

    cat > "$SCRIPT_DIR/.kiro/hooks/re-index-rag.kiro.hook" << EOF
{
  "version": "1.0.0",
  "enabled": true,
  "name": "Re-index RAG",
  "description": "Re-indexes all configured projects in the RAG MCP server.",
  "when": {
    "type": "userTriggered"
  },
  "then": {
    "type": "runCommand",
    "command": "$SCRIPT_DIR/.venv/bin/python $SCRIPT_DIR/indexer.py",
    "timeout": 300
  }
}
EOF

    echo "  Installed: .kiro/hooks/re-index-rag.kiro.hook"
else
    echo "[skip] Kiro not detected (~/.kiro not found), skipping MCP config and hook installation."
    echo "       Install Kiro and re-run setup.sh to enable IDE integration."
fi

echo ""
echo "[verify] Running MCP connection smoke test..."
if "$SCRIPT_DIR/.venv/bin/python" -m pytest "$SCRIPT_DIR/tests/test_mcp_connection.py" -q --tb=short 2>/dev/null; then
    echo "  [OK] All connection tests passed!"
else
    echo "  [WARN] Some connection tests failed. Check server.py and config.yaml."
fi

echo ""
echo "=== Setup complete! ==="
echo ""
echo "PROJECTS_ROOT = $PROJECTS_ROOT"
echo ""
echo "Next steps:"
if [ -d "$HOME/.kiro" ]; then
    echo "  1. Restart Kiro"
    echo "  2. Ask Kiro questions about your projects!"
else
    echo "  1. Configure your MCP client to use:"
    echo "       Command: $SCRIPT_DIR/.venv/bin/python"
    echo "       Args:    $SCRIPT_DIR/server.py"
    echo "  2. Or install Kiro and re-run setup.sh for automatic integration."
fi
echo ""
