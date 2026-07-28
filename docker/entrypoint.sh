#!/bin/sh
# Seed the config file from the bundled template on first run if it isn't
# present yet, then hand off to the container command (server or indexer).
#
# The config path is RAG_CONFIG_PATH (default /app/data/config.yaml, i.e. the
# data volume) so the running container depends only on the image + volumes,
# not on any host repo files.
set -e

CONFIG_FILE="${RAG_CONFIG_PATH:-/app/data/config.yaml}"
TEMPLATE_FILE="/app/config/config.template.yaml"

if [ ! -f "$CONFIG_FILE" ]; then
    if [ -f "$TEMPLATE_FILE" ]; then
        echo "[entrypoint] $CONFIG_FILE not found, creating from template." >&2
        echo "[entrypoint] NOTE: projects list is empty — run discovery/indexing to populate it." >&2
        mkdir -p "$(dirname "$CONFIG_FILE")"
        cp "$TEMPLATE_FILE" "$CONFIG_FILE"
    else
        echo "[entrypoint] ERROR: template not found at $TEMPLATE_FILE" >&2
        exit 1
    fi
fi

exec "$@"
