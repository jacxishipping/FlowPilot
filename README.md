# FlowPilot

Multi-tenant workflow automation and autonomous AI agent platform built around **deterministic orchestration around bounded autonomy**.

## Repository Overview

| Layer | Location | Highlights |
|---|---|---|
| **Core Contracts & Engine** | `packages/workflow-schema`, `packages/expression-engine`, `packages/node-sdk`, `packages/workflow-validator`, `packages/execution-engine`, `nodes/core` | Canonical JSON graph schema, restricted deterministic expression evaluator, typed Python Node SDK with SSRF-safe HTTP client, pre-publish DAG/policy validator, durable graph execution engine, and 12 core nodes |
| **Credential Manager** | `packages/credential-manager` | Tenant-bound AES-256-GCM envelope encryption (per-record DEK + pluggable KMS KEK + key rotation) |
| **FastAPI Backend & Migrations** | `apps/api`, `migrations/` | `/api/v1` REST + SSE API, SQLAlchemy 2.0 schema (23 core tables), PostgreSQL Row-Level Security (RLS) + `pgvector` Alembic migration, optimistic revision concurrency, approval pause/resume, and natural-language workflow draft generation |
| **Temporal Workers** | `apps/workers/workflow` | Temporal workflow & activity workers executing canonical `ActivityTask` envelopes on isolated pool task queues (`flowpilot-{pool}-workers`) |
| **Visual Editor** | `apps/web` | Next.js 15 + React 19 + TypeScript strict + React Flow (`@xyflow/react`) visual canvas consuming `/api/v1/nodes/catalog` and `/api/v1/schemas/workflow`, with drag-and-drop palette, schema-driven inspector, live validation diagnostics, SSE execution log, human approval gates, encrypted vault, and AI draft generator |
| **Docker Compose Stack** | `infrastructure/compose`, `infrastructure/docker` | Self-hosted & local dev stack: PostgreSQL 16 + `pgvector`, Redis 7, Temporal + Temporal UI, MinIO S3, FastAPI API, Temporal workers, and Next.js Studio UI |

## Local Development

### Python Backend & Core Engine

```bash
uv sync
uv run pytest -v
uv run ruff check .
uv run mypy packages nodes apps tests

# Run FastAPI locally (SQLite dev mode or PostgreSQL)
uv run uvicorn flowpilot_api.app:app --reload --port 8000
```

### Next.js Visual Editor

```bash
cd apps/web
npm install
npm run dev
```

### Full Docker Compose Stack

```bash
cp .env.example .env
docker compose -f infrastructure/compose/docker-compose.yml up --build
```

- **Studio UI**: http://localhost:3000
- **FastAPI Docs (OpenAPI)**: http://localhost:8000/docs
- **Temporal UI**: http://localhost:8080
- **MinIO Console**: http://localhost:9001

See [docs/architecture-spec.md](docs/architecture-spec.md) for the technical blueprint.
