# FlowPilot — Technical Product Specification and Implementation Blueprint

**Status:** Initial architecture proposal  
**Product type:** Multi-tenant workflow automation and autonomous AI agent SaaS  
**Working name:** FlowPilot (not trademark-cleared)  
**Deployment:** Managed cloud SaaS and self-hosted edition  
**Primary backend:** Python / FastAPI  
**Frontend:** Next.js / React / TypeScript  

## Core Design Principle

Deterministic orchestration around bounded autonomy. Agents may reason and choose permitted tools, but workflow policies control their permissions, budgets, approvals, and completion conditions.

## Core Packages Built First (Schema & Runtime Boundary)

1. `packages/workflow-schema` — Canonical graph definition, immutable workflow versions, run/node state enums and models.
2. `packages/expression-engine` — Restricted deterministic expression evaluator (`{{ nodes.customer_lookup.output.email }}`).
3. `packages/node-sdk` — Typed Python Node SDK (`flowpilot.node`, `schemas`, `credentials`, `context`, `errors`, `http`, `testing`).
4. `packages/workflow-validator` — Pre-publish structural, data-flow, schema, credential, permission, and loop validation.
5. `packages/execution-engine` — Custom graph interpreter, durable execution coordinator, Temporal adapter interface, retries/idempotency, approvals, cancellation, usage metering.
6. `nodes/core` — First-party core nodes built on the Node SDK.
