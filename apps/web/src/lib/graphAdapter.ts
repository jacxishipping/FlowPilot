/** Bidirectional adapter between canonical WorkflowGraph JSON and React Flow nodes/edges. */

import type { Edge, Node } from "@xyflow/react";
import type {
  EdgeDefinition,
  EdgeKind,
  NodeCatalogEntry,
  NodeDefinition,
  NodeStatus,
  WorkflowGraph,
} from "./types";

export interface FlowNodeData extends Record<string, unknown> {
  nodeDef: NodeDefinition;
  catalog?: NodeCatalogEntry;
  executionStatus?: NodeStatus;
  durationMs?: number | null;
  hasValidationError?: boolean;
}

export type FlowCanvasNode = Node<FlowNodeData, "flowNode">;
export type FlowCanvasEdge = Edge<{ kind: EdgeKind }>;

export function defaultPolicy(): NodeDefinition["policy"] {
  return {
    timeout_ms: 30000,
    retry: {
      max_attempts: 3,
      initial_interval_ms: 1000,
      max_interval_ms: 60000,
      backoff_multiplier: 2.0,
      jitter_ratio: 0.2,
      allow_retry_with_idempotency_key: false,
    },
    idempotency_key_expression: null,
    require_approval: false,
    worker_pool: null,
    allowed_domains: [],
    required_permissions: [],
  };
}

export function edgeColor(kind: EdgeKind): string {
  switch (kind) {
    case "true":
      return "#10b981";
    case "false":
      return "#f59e0b";
    case "error":
      return "#ef4444";
    default:
      return "#64748b";
  }
}

export function canonicalToCanvas(
  graph: WorkflowGraph,
  catalogByType: Map<string, NodeCatalogEntry>,
  nodeStatuses: Record<string, { status: NodeStatus; duration_ms?: number | null }>,
  errorNodeIds: Set<string>,
): { nodes: FlowCanvasNode[]; edges: FlowCanvasEdge[] } {
  const nodes: FlowCanvasNode[] = graph.nodes.map((nodeDef, idx) => ({
    id: nodeDef.id,
    type: "flowNode",
    position: nodeDef.ui?.position ?? { x: 80 + idx * 280, y: 180 },
    data: {
      nodeDef,
      catalog: catalogByType.get(nodeDef.type),
      executionStatus: nodeStatuses[nodeDef.id]?.status,
      durationMs: nodeStatuses[nodeDef.id]?.duration_ms,
      hasValidationError: errorNodeIds.has(nodeDef.id),
    },
  }));

  const edges: FlowCanvasEdge[] = graph.edges.map((edgeDef) => {
    const color = edgeColor(edgeDef.kind);
    return {
      id: edgeDef.id,
      source: edgeDef.source,
      target: edgeDef.target,
      sourceHandle: edgeDef.source_handle ?? (edgeDef.kind === "default" ? "default" : edgeDef.kind),
      targetHandle: edgeDef.target_handle ?? "in",
      label: edgeDef.label ?? (edgeDef.kind === "default" ? undefined : edgeDef.kind.toUpperCase()),
      animated: edgeDef.kind === "error",
      style: { stroke: color, strokeWidth: 2 },
      labelStyle: { fill: color, fontSize: 10, fontWeight: 700 },
      labelBgStyle: { fill: "#0b0e14", fillOpacity: 0.9 },
      data: { kind: edgeDef.kind },
    };
  });

  return { nodes, edges };
}

/** Deterministic layered DAG auto-layout without external layout dependencies. */
export function autoLayoutGraph(graph: WorkflowGraph): WorkflowGraph {
  const inDegree = new Map<string, number>();
  const outgoing = new Map<string, string[]>();
  for (const n of graph.nodes) {
    inDegree.set(n.id, 0);
    outgoing.set(n.id, []);
  }
  for (const e of graph.edges) {
    outgoing.get(e.source)?.push(e.target);
    inDegree.set(e.target, (inDegree.get(e.target) ?? 0) + 1);
  }

  const layerByNode = new Map<string, number>();
  const queue: string[] = [];
  for (const [id, deg] of inDegree.entries()) {
    if (deg === 0) {
      queue.push(id);
      layerByNode.set(id, 0);
    }
  }

  while (queue.length > 0) {
    const u = queue.shift()!;
    const layer = layerByNode.get(u) ?? 0;
    for (const v of outgoing.get(u) ?? []) {
      layerByNode.set(v, Math.max(layerByNode.get(v) ?? 0, layer + 1));
      const nextDeg = (inDegree.get(v) ?? 1) - 1;
      inDegree.set(v, nextDeg);
      if (nextDeg === 0) queue.push(v);
    }
  }

  const buckets = new Map<number, string[]>();
  for (const n of graph.nodes) {
    const layer = layerByNode.get(n.id) ?? 0;
    const arr = buckets.get(layer) ?? [];
    arr.push(n.id);
    buckets.set(layer, arr);
  }

  const positions = new Map<string, { x: number; y: number }>();
  for (const [layer, ids] of buckets.entries()) {
    const startY = 180 - ((ids.length - 1) * 150) / 2;
    ids.forEach((id, rowIdx) => {
      positions.set(id, { x: 80 + layer * 300, y: Math.max(40, startY + rowIdx * 155) });
    });
  }

  return {
    ...graph,
    nodes: graph.nodes.map((n) => ({
      ...n,
      ui: {
        ...(n.ui ?? { position: { x: 0, y: 0 } }),
        position: positions.get(n.id) ?? n.ui?.position ?? { x: 80, y: 180 },
      },
    })),
  };
}

export function createStarterWorkflowGraph(): WorkflowGraph {
  const nodes: NodeDefinition[] = [
    {
      id: "trigger",
      type: "core.manual_trigger",
      name: "Order Intake Trigger",
      description: "Starts when an order payload is submitted",
      config: { defaults: { amount: 250, customer_email: "buyer@acme.io" } },
      credentials: {},
      policy: defaultPolicy(),
      ui: { position: { x: 60, y: 180 } },
    },
    {
      id: "high_value_check",
      type: "core.condition",
      name: "Amount >= 200?",
      description: "Routes high-value orders through human approval",
      config: { condition: "{{ nodes.trigger.output.amount >= 200 }}" },
      credentials: {},
      policy: defaultPolicy(),
      ui: { position: { x: 360, y: 180 } },
    },
    {
      id: "human_approval",
      type: "core.human_approval",
      name: "Finance Approval Gate",
      description: "Pauses durable run until finance approves",
      config: {
        title: "Approve order for {{ nodes.trigger.output.customer_email }}",
        description: "High-value order requires sign-off",
        risk_level: "high",
        payload: { amount: "{{ nodes.trigger.output.amount }}" },
        fail_on_reject: false,
      },
      credentials: {},
      policy: defaultPolicy(),
      ui: { position: { x: 680, y: 90 } },
    },
    {
      id: "auto_fulfill",
      type: "core.set_data",
      name: "Auto Fulfill",
      description: "Immediate fulfillment for low-risk orders",
      config: {
        values: {
          status: "auto_fulfilled",
          email: "{{ nodes.trigger.output.customer_email | lower }}",
        },
      },
      credentials: {},
      policy: defaultPolicy(),
      ui: { position: { x: 680, y: 280 } },
    },
    {
      id: "approved_fulfill",
      type: "core.set_data",
      name: "Fulfill Approved Order",
      description: "Completes order after human approval",
      config: {
        values: {
          status: "approved_and_fulfilled",
          approver: "{{ nodes.human_approval.output.decided_by }}",
        },
      },
      credentials: {},
      policy: defaultPolicy(),
      ui: { position: { x: 1000, y: 90 } },
    },
  ];

  const edges: EdgeDefinition[] = [
    { id: "e_trig_cond", source: "trigger", target: "high_value_check", kind: "default" },
    {
      id: "e_cond_true",
      source: "high_value_check",
      target: "human_approval",
      kind: "true",
      label: "TRUE",
    },
    {
      id: "e_cond_false",
      source: "high_value_check",
      target: "auto_fulfill",
      kind: "false",
      label: "FALSE",
    },
    {
      id: "e_app_true",
      source: "human_approval",
      target: "approved_fulfill",
      kind: "true",
      label: "APPROVED",
    },
  ];

  return { nodes, edges };
}
