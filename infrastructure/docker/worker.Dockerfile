# FlowPilot Temporal worker image (non-root, isolated by FLOWPILOT_WORKER_POOL)
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLOWPILOT_WORKER_POOL=workflow

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml ./
COPY packages ./packages
COPY nodes ./nodes
COPY apps/api ./apps/api
COPY apps/scheduler ./apps/scheduler
COPY apps/workers/workflow ./apps/workers/workflow

RUN uv sync --no-dev --frozen || uv sync --no-dev

RUN useradd --create-home --shell /bin/bash flowpilot && chown -R flowpilot:flowpilot /app
USER flowpilot

CMD ["uv", "run", "python", "-c", "from flowpilot_worker_workflow import main; main()"]
