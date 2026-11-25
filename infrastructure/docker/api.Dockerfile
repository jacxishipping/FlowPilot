# FlowPilot FastAPI backend image (non-root, uv workspace)
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1

RUN pip install --no-cache-dir uv

WORKDIR /app

# Copy workspace manifests & source packages
COPY pyproject.toml uv.lock alembic.ini ./
COPY packages ./packages
COPY nodes ./nodes
COPY apps/api ./apps/api
COPY apps/cli ./apps/cli
COPY apps/scheduler ./apps/scheduler
COPY apps/workers/workflow ./apps/workers/workflow
COPY apps/workers/code ./apps/workers/code
COPY apps/workers/browser ./apps/workers/browser
COPY migrations ./migrations

RUN uv sync --no-dev --frozen || uv sync --no-dev

# Run as non-root user
RUN useradd --create-home --shell /bin/bash flowpilot && chown -R flowpilot:flowpilot /app
USER flowpilot

EXPOSE 8000

CMD ["sh", "-c", "uv run alembic upgrade head && uv run uvicorn flowpilot_api.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
