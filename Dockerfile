#
# Multi-stage build for rag-mcp MCP server.
#
# Stage 1 (builder): installs all deps including build tools, downloads the
#   embedding model, then strips test/dev packages from the site-packages.
# Stage 2 (runtime): copies only the cleaned site-packages + app code into a
#   fresh slim image — no pip, no compilers, no test deps.
#
#   Build:  docker build -t wsl --update rag-mcp-new-pip:latest .
#   Index:  docker compose run --rm indexer
#   Serve:  docker run -i --rm \
#             -v /host/git:/projects:ro \
#             -v rag-mcp-new-pip-data:/app/data \
#            rag-mcp-new-pip:latest

# ---------------------------------------------------------------------------
# Stage 1 — builder
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/opt/hf-cache \
    EMBED_MODEL=BAAI/bge-small-en-v1.5 \
    RERANK_MODEL=cross-encoder/ms-marco-MiniLM-L-6-v2

WORKDIR /build

# Install runtime requirements (CPU-only Torch first to avoid the CUDA build).
COPY requirements.txt ./
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch \
 && pip install -r requirements.txt

# Remove test/dev packages — they must not reach the runtime image.
RUN pip uninstall -y pytest pytest-asyncio hypothesis 2>/dev/null || true

# Pre-download and cache the embedding + reranker models inside the image.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('${EMBED_MODEL}')" \
 && python -c "from sentence_transformers import CrossEncoder; CrossEncoder('${RERANK_MODEL}')"

# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/hf-cache \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    RAG_CONFIG_PATH=/app/data/config.yaml \
    EMBED_MODEL=BAAI/bge-small-en-v1.5 \
    RERANK_MODEL=cross-encoder/ms-marco-MiniLM-L-6-v2

WORKDIR /app

# Copy the cleaned Python site-packages from builder.
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy baked HF model cache.
COPY --from=builder /opt/hf-cache /opt/hf-cache

# Application code — server/indexer runtime plus setup scripts (used by
# setup-docker.bat/.sh at first-run time: discovery + mcp.json generation).
COPY server.py indexer.py ./
COPY src/ ./src/
COPY config/ ./config/
COPY scripts/ ./scripts/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

VOLUME ["/app/data", "/projects"]
EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "server.py"]
