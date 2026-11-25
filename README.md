# FlowPilot

Multi-tenant workflow automation and autonomous AI agent platform built around **deterministic orchestration around bounded autonomy**.

## Repository Overview

| Layer | Location | Highlights |
|---|---|---|
| **Core Contracts & Engine** | `packages/workflow-schema`, `packages/expression-engine`, `packages/node-sdk`, `packages/workflow-validator`, `packages/execution-engine`, `nodes/core`, `nodes/connectors` | Canonical JSON graph schema, restricted deterministic expression evaluator, typed Python Node SDK with SSRF-safe HTTP client & Ed25519 package signing, pre-publish DAG/policy validator, durable graph execution engine with Time-Travel Replay, Workflow Evals, reusable Sub-Workflows, synchronous Webhook Responses, and **25 first-party nodes** (14 core + Email, Slack, SQL, GitHub, Linear, Notion, Google Sheets, Stripe, HubSpot, Python Sandbox, Browser) |
| **Agent Runtime & MCP Bridge** | `packages/agent-runtime`, `packages/policy-engine`, `packages/observability` | Bounded AI agents with OpenAI/Anthropic/Gemini providers, `AgentToolGateway`, outbound & inbound JSON-RPC 2.0 **MCP Bridge**, pre-LLM PII scrubbing, prompt-injection detection, JSON output schema enforcement, `pgvector` semantic memory, RBAC, and OpenTelemetry/Prometheus telemetry |
| **Credential Manager** | `packages/credential-manager` | Tenant-bound AES-256-GCM envelope encryption (per-record DEK + pluggable AWS/GCP/Vault/Local KMS KEK + Tenant CMK + key rotation) |
| **FastAPI Backend & Migrations** | `apps/api`, `migrations/` | 63 OpenAPI `/api/v1` REST + SSE + MCP endpoints, SQLAlchemy 2.0 schema (23 core tables), PostgreSQL `FORCE ROW LEVEL SECURITY` + `pgvector` Alembic migration, environment promotion (`dev` → `staging` → `prod`), version diff & rollback, Approvals Inbox + signed Slack/email one-click action links, curated Templates Gallery, SSO (OIDC/SAML), SCIM 2.0, and SIEM audit export (NDJSON/CEF) |
| **Flow-as-Code CLI** | `apps/cli` | `flowpilot` GitOps CLI (`validate`, `test`, `diff`, `pull`, `publish`) for versioning canonical workflow JSON in Git |
| **Temporal Workers & Scheduler** | `apps/workers/workflow`, `apps/workers/code`, `apps/workers/browser`, `apps/scheduler` | Temporal workflow & isolated pool activity workers (`workflow`, `code`, `browser`) plus cron schedule poller |
| **Visual Studio** | `apps/web` | Next.js 15 + React 19 + TypeScript strict + React Flow (`@xyflow/react`) visual canvas with Live Data Pinning, clickable Expression Autocomplete + live preview, Time-Travel "Fork & Replay from Node", Visual Version Diff & One-Click Rollback, Approvals Inbox, Curated Templates Gallery, SSE execution log, encrypted vault, and AI draft generator |
| **Infrastructure & CI** | `infrastructure/compose`, `infrastructure/docker`, `infrastructure/k8s`, `infrastructure/terraform`, `.github/workflows/ci.yml` | Docker Compose dev stack, Kubernetes manifests, Prometheus alerts, Terraform AWS baseline, plus GitHub Actions CI with real PostgreSQL `FORCE ROW LEVEL SECURITY` + `pgvector` integration tests and live Playwright E2E |

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
