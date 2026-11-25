/** Canonical TypeScript contracts matching flowpilot_schema & FastAPI /api/v1 */

export type EdgeKind = "default" | "true" | "false" | "error";

export type RunStatus =
  | "QUEUED"
  | "RUNNING"
  | "WAITING_APPROVAL"
  | "WAITING_RETRY"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELED"
  | "TIMED_OUT";

export type NodeStatus =
  | "PENDING"
  | "READY"
  | "RUNNING"
  | "SUCCEEDED"
  | "FAILED"
  | "SKIPPED"
  | "RETRYING"
  | "CANCELED";

export interface Position {
  x: number;
  y: number;
}

export interface NodeUI {
  position: Position;
  collapsed?: boolean;
  color?: string | null;
  notes?: string | null;
}

export interface RetryPolicy {
  max_attempts: number;
  initial_interval_ms: number;
  max_interval_ms: number;
  backoff_multiplier: number;
  jitter_ratio: number;
  allow_retry_with_idempotency_key: boolean;
}

export interface NodePolicy {
  timeout_ms: number;
  retry: RetryPolicy;
  idempotency_key_expression: string | null;
  require_approval: boolean;
  worker_pool: string | null;
  allowed_domains: string[];
  required_permissions: string[];
}

export interface LoopSpec {
  items_expression: string;
  item_variable: string;
  index_variable: string;
  max_iterations: number;
  fail_fast: boolean;
  body_graph: WorkflowGraph;
}

export interface NodeDefinition {
  id: string;
  type: string;
  name: string;
  description: string;
  node_version?: string | null;
  config: Record<string, unknown>;
  credentials: Record<string, string>;
  policy: NodePolicy;
  loop?: LoopSpec | null;
  disabled?: boolean;
  ui?: NodeUI | null;
}

export interface EdgeDefinition {
  id: string;
  source: string;
  target: string;
  kind: EdgeKind;
  source_handle?: string | null;
  target_handle?: string | null;
  label?: string | null;
}

export interface WorkflowGraph {
  nodes: NodeDefinition[];
  edges: EdgeDefinition[];
}

export interface WorkflowDefinition {
  schema_version: "1.0.0";
  id?: string;
  organization_id?: string;
  project_id?: string;
  name: string;
  description: string;
  revision: number;
  graph: WorkflowGraph;
  output_mapping?: Record<string, unknown> | null;
  tags?: string[];
}

export interface CredentialRequirement {
  slot: string;
  credential_type: string;
  required: boolean;
  scopes: string[];
  description: string;
}

export interface NodeMetadata {
  type: string;
  name: string;
  description: string;
  category: string;
  kind: string;
  version: string;
  worker_pool: string;
  side_effect: string;
  risk_level: string;
  credentials: CredentialRequirement[];
  required_permissions: string[];
  output_branches: string[];
  is_loop: boolean;
}

export interface JsonSchemaProperty {
  type?: string;
  title?: string;
  description?: string;
  default?: unknown;
  enum?: string[];
  minimum?: number;
  maximum?: number;
  anyOf?: Array<{ type?: string }>;
}

export interface JsonSchemaObject {
  title?: string;
  type?: string;
  properties?: Record<string, JsonSchemaProperty>;
  required?: string[];
}

export interface NodeCatalogEntry {
  metadata: NodeMetadata;
  config_schema: JsonSchemaObject;
  output_schema: JsonSchemaObject;
}

export interface ValidationIssue {
  code: string;
  severity: "error" | "warning";
  message: string;
  node_id?: string | null;
  edge_id?: string | null;
  path?: string | null;
}

export interface ValidationResult {
  issues: ValidationIssue[];
}

export interface WorkflowRecord {
  id: string;
  organization_id: string;
  project_id: string;
  name: string;
  description: string;
  revision: number;
  is_active: boolean;
  active_version_id: string | null;
  definition: WorkflowDefinition;
  created_at: string;
  updated_at: string;
}

export interface ApprovalRequest {
  id: string;
  run_id: string;
  node_id: string;
  title: string;
  description: string;
  risk_level: string;
  decision: "pending" | "approved" | "rejected" | "expired";
  decided_by?: string | null;
  comment?: string | null;
  payload: Record<string, unknown>;
}

export interface NodeExecutionState {
  id: string;
  node_id: string;
  node_type: string;
  status: NodeStatus;
  attempt: number;
  duration_ms?: number | null;
  branch_taken?: string | null;
  output_ref?: { inline?: unknown; artifact?: { uri: string; size_bytes: number } } | null;
  error?: { code: string; message: string } | null;
}

export interface WorkflowRunState {
  id: string;
  workflow_id: string;
  workflow_version_id: string;
  status: RunStatus;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  node_executions: Record<string, NodeExecutionState>;
  approvals: Record<string, ApprovalRequest>;
  error?: { code: string; message: string } | null;
}

export interface ExecutionEvent {
  id: string;
  sequence: number;
  event_type: string;
  node_id?: string | null;
  timestamp: string;
  payload: Record<string, unknown>;
}

export interface CredentialMeta {
  id: string;
  name: string;
  credential_type: string;
  key_id: string;
  scopes: string[];
  created_at: string;
}

export interface BootstrapSession {
  organization_id: string;
  workspace_id: string;
  project_id: string;
  api_key: string;
}
