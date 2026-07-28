#!/bin/bash
# RAG MCP Server - Docker setup (Phase 1) for Linux/macOS
# Builds the image, generates a docker config (container paths), indexes all
# sibling projects, and wires up mcp.json to launch the server via docker.
#
# Requires: Docker. No local Python needed.

set -e

echo "=== RAG MCP Server - Docker Setup ==="
echo ""

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECTS_ROOT="$(dirname "$SCRIPT_DIR")"

echo "Detected PROJECTS_ROOT: $PROJECTS_ROOT"
echo "Repo path:            $SCRIPT_DIR"
echo ""

# Verify Docker is available
if ! docker version >/dev/null 2>&1; then
    echo "ERROR: Docker not found or not running. Start Docker and retry."
    exit 1
fi

echo "[1/4] Building image rag-mcp-new-pip:latest ..."
docker build -t rag-mcp-new-pip:latest "$SCRIPT_DIR"

echo ""
echo "[2/4] Discovering projects (container paths under /projects) ..."
# Runs inside the container: discovery seeds config.docker.yaml from the
# template, scans /projects, and writes base_path=/projects/<repo>. The
# Discovery writes the config INTO the data volume (/app/data/config.yaml),
# seeding from the image's bundled template. The running server reads it from
# there (RAG_CONFIG_PATH), so no repo files are needed at runtime.
# Selection is done on the HOST (no in-container TTY needed): list folders,
# prompt here, then run discovery non-interactively with --select.
docker run --rm \
    -v "$PROJECTS_ROOT":/projects:ro \
    -v rag-mcp-new-pip-data:/app/data \
    rag-mcp-new-pip:latest \
    python scripts/setup_discover.py /projects --config /app/data/config.yaml --list

echo ""
read -r -p "Select folders to index (comma-separated numbers, 'all', or 'none') [all]: " SELECTION
SELECTION="${SELECTION:-all}"

docker run --rm \
    -v "$PROJECTS_ROOT":/projects:ro \
    -v rag-mcp-new-pip-data:/app/data \
    rag-mcp-new-pip:latest \
    python scripts/setup_discover.py /projects --config /app/data/config.yaml --select "$SELECTION" \
    || echo "WARNING: Discovery had issues."

echo ""
echo "[3/4] Indexing selected projects into volume rag-mcp-new-pip-data ..."
docker run --rm \
    -v "$PROJECTS_ROOT":/projects:ro \
    -v rag-mcp-new-pip-data:/app/data \
    rag-mcp-new-pip:latest \
    python indexer.py \
    || echo "WARNING: Indexing had issues. Review the output above."

echo ""
echo "[4/4] Updating mcp.json for Kiro (docker command) ..."
if [ -d "$HOME/.kiro" ]; then
    mkdir -p "$HOME/.kiro/settings"
    # Update the user-level mcp.json by mounting ~/.kiro into the container.
    docker run --rm \
        -v "$HOME/.kiro":/hostkiro \
        rag-mcp-new-pip:latest \
        python scripts/setup_mcp_config.py --docker \
            --projects-dir "$PROJECTS_ROOT" \
            --image rag-mcp-new-pip:latest \
            --data-volume rag-mcp-new-pip-data \
            --out /hostkiro/settings/mcp.json
else
    echo "[skip] Kiro not detected (~/.kiro not found), skipping mcp.json update."
fi

echo ""
echo "=== Docker setup complete! ==="
echo ""
echo "Image:         rag-mcp-new-pip:latest"
echo "Config:        $SCRIPT_DIR/config/config.yaml"
echo "Data volume:   rag-mcp-new-pip-data"
echo ""
echo "Next steps:"
echo "  1. Restart Kiro"
echo "  2. Ask Kiro questions about your projects!"
echo ""
echo "To re-index later:"
echo "  PROJECTS_DIR=\"$PROJECTS_ROOT\" docker compose run --rm indexer"
echo ""
