# Untested build: Docker was not available where this was written. Build it on Day 3 (milestone gate).
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
RUN pip install "uv>=0.5" && useradd -m -u 10001 bridge
COPY requirements.txt .
RUN pip install -r requirements.txt            # claude-agent-sdk bundles its own CLI; no Node needed
COPY vendor ./vendor
COPY skill ./skill
COPY bridge ./bridge
COPY scripts ./scripts
RUN mkdir -p /app/data && chown -R bridge:bridge /app
USER bridge
# Pre-build the vendor MCP's environment so the first question doesn't download packages.
RUN uv sync --directory /app/vendor/ris-claude-gateway --frozen
ENV MCP_DIR=/app/vendor/ris-claude-gateway DB_PATH=/app/data/bridge.db
VOLUME /app/data
CMD ["python", "-m", "bridge.main"]
