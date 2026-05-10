FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Upgrade pip to address CVE-2026-6357. CVE-2025-8869 N/A on Python 3.12 (PEP 706).
RUN pip install --no-cache-dir --upgrade "pip>=26.1"

RUN pip install --no-cache-dir uv

# Install runtime deps first (without the project itself) — this layer is
# cached and only invalidates when pyproject.toml or uv.lock change.
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev --no-install-project \
 || uv sync --no-dev --no-install-project

# Now bring in the project sources and install the wire package itself.
COPY README.md alembic.ini ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev || uv sync --no-dev

ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -fsS http://localhost:8080/health || exit 1

CMD ["python", "-m", "wire.main"]
