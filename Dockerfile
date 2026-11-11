# Khata: the agents (`khata serve`) and the read-only dashboard (`khata dashboard`).
# Switch, Mattermost and Postgres run from their own images (docker-compose.yml).
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 \
    FASTEMBED_CACHE_PATH=/app/.cache/fastembed

# Tesseract reads scanned and photographed invoices (khata/ocr.py).
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY khata ./khata
COPY db ./db
COPY data ./data
COPY evals ./evals
COPY switch ./switch
COPY scripts ./scripts
COPY mcp_server ./mcp_server
RUN uv sync --frozen --no-dev

# The embedding model (bge-small, ~70 MB) is baked in so the Analyst never waits on a download.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"

RUN useradd --create-home --uid 10001 khata && mkdir -p /app/state /app/data/out /app/data/inbox \
    && chown -R khata /app
USER khata

CMD ["khata", "-v", "serve"]
