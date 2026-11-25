/** Typed FlowPilot /api/v1 client with SSE event parsing and fallback offline node catalog. */

import type {
  BootstrapSession,
  CredentialMeta,
  ExecutionEvent,
  NodeCatalogEntry,
  ValidationResult,
  WorkflowDefinition,
  WorkflowGraph,
  WorkflowRecord,
  WorkflowRunState,
} from "./types";

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") || "http://localhost:8000";

export class ApiError extends Error {
  status: number;
  details: unknown;

  constructor(message: string, status: number, details?: unknown) {
    super(message);
    this.status = status;
    this.details = details;
  }
}

async function apiFetch<T>(
  path: string,
  options: RequestInit & { apiKey?: string } = {},
): Promise<T> {
  const { apiKey, headers, ...rest } = options;
  const reqHeaders: Record<string, string> = {
    "Content-Type": "application/json",
    ...(headers as Record<string, string> | undefined),
  };
  if (apiKey) {
    reqHeaders["Authorization"] = `Bearer ${apiKey}`;
  }

  const res = await fetch(`${API_BASE_URL}${path}`, {
    ...rest,
    headers: reqHeaders,
  });

  if (!res.ok) {
    let payload: unknown = null;
    try {
      payload = await res.json();
    } catch {
      payload = await res.text();
    }
    const errMsg =
      typeof payload === "object" &&
      payload !== null &&
      "error" in payload &&
      typeof (payload as { error?: { message?: string } }).error?.message === "string"
        ? (payload as { error: { message: string } }).error.message
        : `HTTP ${res.status}`;
    throw new ApiError(errMsg, res.status, payload);
  }

  return (await res.json()) as T;
}

export async function fetchNodeCatalog(): Promise<NodeCatalogEntry[]> {
  const data = await apiFetch<{ nodes: NodeCatalogEntry[] }>("/api/v1/nodes/catalog");
  return data.nodes;
}

export async function fetchWorkflowJsonSchema(): Promise<Record<string, unknown>> {
  return apiFetch<Record<string, unknown>>("/api/v1/schemas/workflow");
}

export async function bootstrapDevTenant(slug: string): Promise<BootstrapSession> {
  return apiFetch<BootstrapSession>("/api/v1/auth/bootstrap", {
    method: "POST",
    body: JSON.stringify({
      email: `founder+${slug}@flowpilot.dev`,
      full_name: "Studio Operator",
      password: "flowpilot-dev-password",
      organization_name: `FlowPilot Studio (${slug})`,
      organization_slug: slug,
      workspace_name: "Default Workspace",
      project_name: "Automation Studio",
    }),
  });
}

export async function createWorkflow(
  apiKey: string,
  payload: { project_id: string; name: string; description: string; graph: WorkflowGraph },
): Promise<WorkflowRecord> {
  return apiFetch<WorkflowRecord>("/api/v1/workflows", {
    method: "POST",
    apiKey,
    body: JSON.stringify(payload),
  });
}

export async function patchWorkflow(
  apiKey: string,
  workflowId: string,
  payload: {
    expected_revision: number;
    name?: string;
    description?: string;
    graph?: WorkflowGraph;
  },
): Promise<WorkflowRecord> {
  return apiFetch<WorkflowRecord>(`/api/v1/workflows/${workflowId}`, {
    method: "PATCH",
    apiKey,
    body: JSON.stringify(payload),
  });
}

export async function validateWorkflowApi(
  apiKey: string,
  workflowId: string,
): Promise<ValidationResult> {
  return apiFetch<ValidationResult>(`/api/v1/workflows/${workflowId}/validate`, {
    method: "POST",
    apiKey,
  });
}

export async function publishWorkflowApi(
  apiKey: string,
  workflowId: string,
  changelog = "Published from FlowPilot Studio",
): Promise<{ id: string; version_number: number; content_hash: string }> {
  return apiFetch(`/api/v1/workflows/${workflowId}/publish`, {
    method: "POST",
    apiKey,
    body: JSON.stringify({ changelog, activate: true }),
  });
}

export async function startWorkflowRunApi(
  apiKey: string,
  workflowId: string,
  triggerInput: Record<string, unknown>,
): Promise<WorkflowRunState> {
  return apiFetch<WorkflowRunState>(`/api/v1/workflows/${workflowId}/runs`, {
    method: "POST",
    apiKey,
    body: JSON.stringify({ trigger_input: triggerInput, trigger_type: "manual" }),
  });
}

export async function fetchRunEventsSse(
  apiKey: string,
  runId: string,
): Promise<ExecutionEvent[]> {
  const res = await fetch(`${API_BASE_URL}/api/v1/runs/${runId}/events`, {
    headers: { Authorization: `Bearer ${apiKey}` },
  });
  if (!res.ok) {
    throw new ApiError(`Failed to load SSE events (${res.status})`, res.status);
  }
  const text = await res.text();
  const events: ExecutionEvent[] = [];
  for (const frame of text.split("\n\n")) {
    const dataLine = frame
      .split("\n")
      .find((line) => line.startsWith("data: "));
    if (dataLine) {
      try {
        events.push(JSON.parse(dataLine.slice(6)) as ExecutionEvent);
      } catch {
        // ignore malformed frame
      }
    }
  }
  return events;
}

export async function submitApprovalDecisionApi(
  apiKey: string,
  approvalId: string,
  approved: boolean,
  comment: string,
): Promise<WorkflowRunState> {
  return apiFetch<WorkflowRunState>(`/api/v1/approvals/${approvalId}/decision`, {
    method: "POST",
    apiKey,
    body: JSON.stringify({ approved, comment }),
  });
}

export async function listCredentialsApi(apiKey: string): Promise<CredentialMeta[]> {
  return apiFetch<CredentialMeta[]>("/api/v1/credentials", { apiKey });
}

export async function createCredentialApi(
  apiKey: string,
  payload: { name: string; credential_type: string; secrets: Record<string, string> },
): Promise<CredentialMeta> {
  return apiFetch<CredentialMeta>("/api/v1/credentials", {
    method: "POST",
    apiKey,
    body: JSON.stringify(payload),
  });
}

export async function generateWorkflowDraftApi(
  apiKey: string,
  projectId: string,
  prompt: string,
): Promise<{
  draft: WorkflowDefinition;
  validation: ValidationResult;
  missing_configuration: string[];
  notes: string[];
}> {
  return apiFetch("/api/v1/ai/workflow-generation", {
    method: "POST",
    apiKey,
    body: JSON.stringify({ project_id: projectId, prompt }),
  });
}

/** Fallback local node catalog so canvas renders cleanly even when API is offline. */
export const FALLBACK_NODE_CATALOG: NodeCatalogEntry[] = [
  {
    metadata: {
      type: "core.manual_trigger",
      name: "Manual Trigger",
      description: "Starts a workflow manually with an optional JSON payload.",
      category: "triggers",
      kind: "trigger",
      version: "1.0.0",
      worker_pool: "workflow",
      side_effect: "none",
      risk_level: "low",
      credentials: [],
      required_permissions: [],
      output_branches: [],
      is_loop: false,
    },
    config_schema: {
      properties: { defaults: { type: "object", title: "Defaults", default: {} } },
    },
    output_schema: {},
  },
  {
    metadata: {
      type: "core.webhook_trigger",
      name: "Webhook Trigger",
      description: "Starts when an authenticated webhook request arrives.",
      category: "triggers",
      kind: "trigger",
      version: "1.0.0",
      worker_pool: "workflow",
      side_effect: "none",
      risk_level: "low",
      credentials: [],
      required_permissions: [],
      output_branches: [],
      is_loop: false,
    },
    config_schema: {
      properties: {
        path: { type: "string", default: "/hooks/incoming" },
        method: { type: "string", enum: ["GET", "POST", "PUT", "PATCH", "DELETE"], default: "POST" },
        require_signature: { type: "boolean", default: true },
      },
    },
    output_schema: {},
  },
  {
    metadata: {
      type: "core.set_data",
      name: "Set Data",
      description: "Emits a structured JSON object computed from expressions.",
      category: "data",
      kind: "action",
      version: "1.0.0",
      worker_pool: "workflow",
      side_effect: "none",
      risk_level: "low",
      credentials: [],
      required_permissions: [],
      output_branches: [],
      is_loop: false,
    },
    config_schema: {
      properties: { values: { type: "object", title: "Values", default: {} } },
    },
    output_schema: {},
  },
  {
    metadata: {
      type: "core.condition",
      name: "Condition (If / Else)",
      description: "Routes execution to true or false branches.",
      category: "control",
      kind: "control",
      version: "1.0.0",
      worker_pool: "workflow",
      side_effect: "none",
      risk_level: "low",
      credentials: [],
      required_permissions: [],
      output_branches: ["true", "false"],
      is_loop: false,
    },
    config_schema: {
      required: ["condition"],
      properties: { condition: { type: "string", title: "Condition Expression" } },
    },
    output_schema: {},
  },
  {
    metadata: {
      type: "core.http_request",
      name: "HTTP Request",
      description: "SSRF-protected outbound HTTP request with domain policies.",
      category: "http",
      kind: "action",
      version: "1.0.0",
      worker_pool: "integrations",
      side_effect: "idempotent",
      risk_level: "medium",
      credentials: [
        {
          slot: "auth",
          credential_type: "http_auth",
          required: false,
          scopes: [],
          description: "Optional API key or Bearer token credential.",
        },
      ],
      required_permissions: ["http.outbound"],
      output_branches: [],
      is_loop: false,
    },
    config_schema: {
      required: ["url"],
      properties: {
        method: { type: "string", enum: ["GET", "POST", "PUT", "PATCH", "DELETE"], default: "GET" },
        url: { type: "string", title: "URL" },
        headers: { type: "object", default: {} },
        fail_on_http_error: { type: "boolean", default: true },
      },
    },
    output_schema: {},
  },
  {
    metadata: {
      type: "core.human_approval",
      name: "Human Approval Gate",
      description: "Pauses durable run until an authorized human approves or rejects.",
      category: "control",
      kind: "approval",
      version: "1.0.0",
      worker_pool: "workflow",
      side_effect: "none",
      risk_level: "high",
      credentials: [],
      required_permissions: [],
      output_branches: ["true", "false"],
      is_loop: false,
    },
    config_schema: {
      required: ["title"],
      properties: {
        title: { type: "string", title: "Title" },
        description: { type: "string", title: "Description" },
        risk_level: { type: "string", enum: ["low", "medium", "high", "critical"], default: "high" },
        fail_on_reject: { type: "boolean", default: false },
      },
    },
    output_schema: {},
  },
  {
    metadata: {
      type: "core.ai_prompt",
      name: "AI Model Prompt",
      description: "Invokes an LLM with token/cost metering and budget enforcement.",
      category: "ai",
      kind: "ai",
      version: "1.0.0",
      worker_pool: "agents",
      side_effect: "none",
      risk_level: "low",
      credentials: [],
      required_permissions: ["ai.invoke"],
      output_branches: [],
      is_loop: false,
    },
    config_schema: {
      required: ["prompt"],
      properties: {
        provider: { type: "string", default: "openai" },
        model: { type: "string", default: "gpt-4o-mini" },
        prompt: { type: "string", title: "Prompt" },
        max_tokens: { type: "integer", default: 1024 },
      },
    },
    output_schema: {},
  },
  {
    metadata: {
      type: "core.bounded_agent",
      name: "Bounded AI Agent",
      description: "Bounded autonomous agent with tool allowlist & cost/step budgets.",
      category: "ai",
      kind: "agent",
      version: "1.0.0",
      worker_pool: "agents",
      side_effect: "irreversible",
      risk_level: "high",
      credentials: [],
      required_permissions: ["ai.agent.run"],
      output_branches: [],
      is_loop: false,
    },
    config_schema: {
      required: ["objective"],
      properties: {
        objective: { type: "string", title: "Objective" },
        model: { type: "string", default: "gpt-4o" },
        max_steps: { type: "integer", default: 6 },
        max_cost_usd: { type: "number", default: 2.0 },
      },
    },
    output_schema: {},
  },
];
