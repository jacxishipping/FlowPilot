"use client";

import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { FlowCanvasNode } from "@/lib/graphAdapter";

const CATEGORY_COLORS: Record<string, string> = {
  triggers: "#06b6d4",
  data: "#6366f1",
  control: "#f59e0b",
  http: "#3b82f6",
  ai: "#a855f7",
  connectors: "#10b981",
  communication: "#ec4899",
};

function statusBadgeClass(status?: string): string {
  switch (status) {
    case "SUCCEEDED":
      return "pill pill-success";
    case "FAILED":
    case "CANCELED":
      return "pill pill-danger";
    case "RUNNING":
    case "READY":
    case "RETRYING":
      return "pill pill-info";
    case "SKIPPED":
      return "pill pill-warning";
    default:
      return "pill";
  }
}

export function FlowNode({ data, selected }: NodeProps<FlowCanvasNode>) {
  const { nodeDef, catalog, executionStatus, durationMs, hasValidationError, diffBadge, isPinned } =
    data;
  const category = catalog?.metadata.category ?? "core";
  const accent = CATEGORY_COLORS[category] ?? "#6366f1";
  const isTrigger = catalog?.metadata.kind === "trigger";
  const branches = catalog?.metadata.output_branches ?? [];

  return (
    <div
      className={`fp-node ${selected ? "selected" : ""}`}
      style={{
        borderColor: hasValidationError ? "var(--danger)" : undefined,
      }}
    >
      {!isTrigger && (
        <Handle
          id="in"
          type="target"
          position={Position.Left}
          style={{
            width: 9,
            height: 9,
            background: "var(--border-strong)",
            border: "2px solid var(--handle-border)",
          }}
        />
      )}

      <div className="fp-node-header">
        <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
          <span className="fp-node-dot" style={{ background: accent }} />
          <div style={{ minWidth: 0 }}>
            <div
              style={{
                fontWeight: 600,
                fontSize: 12.5,
                letterSpacing: "-0.01em",
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
              }}
            >
              {nodeDef.name || catalog?.metadata.name || nodeDef.id}
            </div>
            <div
              style={{
                fontSize: 10.5,
                color: "var(--text-muted)",
                fontFamily: "var(--font-mono), monospace",
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
              }}
            >
              {nodeDef.id} · {nodeDef.type}
            </div>
          </div>
        </div>
        {executionStatus && (
          <span className={statusBadgeClass(executionStatus)}>{executionStatus}</span>
        )}
      </div>

      <div className="fp-node-body">
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4, alignItems: "center" }}>
          <span className="pill">{category}</span>
          {nodeDef.policy.require_approval && <span className="pill pill-warning">approval</span>}
          {Object.keys(nodeDef.credentials).length > 0 && (
            <span className="pill pill-info">vault</span>
          )}
          {hasValidationError && <span className="pill pill-danger">invalid</span>}
          {isPinned && <span className="pill pill-info">pinned</span>}
          {diffBadge && (
            <span
              className={
                diffBadge === "added"
                  ? "pill pill-success"
                  : diffBadge === "modified"
                    ? "pill pill-warning"
                    : "pill pill-danger"
              }
            >
              {diffBadge}
            </span>
          )}
          {typeof durationMs === "number" && (
            <span
              style={{
                marginLeft: "auto",
                fontSize: 10.5,
                color: "var(--text-muted)",
                fontFamily: "var(--font-mono), monospace",
              }}
            >
              {durationMs}ms
            </span>
          )}
        </div>
      </div>

      {branches.length > 0 ? (
        <>
          <Handle
            id="true"
            type="source"
            position={Position.Right}
            style={{
              top: "36%",
              width: 9,
              height: 9,
              background: "#10b981",
              border: "2px solid var(--handle-border)",
            }}
          />
          <Handle
            id="false"
            type="source"
            position={Position.Right}
            style={{
              top: "68%",
              width: 9,
              height: 9,
              background: "#f59e0b",
              border: "2px solid var(--handle-border)",
            }}
          />
        </>
      ) : (
        <Handle
          id="default"
          type="source"
          position={Position.Right}
          style={{
            top: "50%",
            width: 9,
            height: 9,
            background: accent,
            border: "2px solid var(--handle-border)",
          }}
        />
      )}

      {!isTrigger && (
        <Handle
          id="error"
          type="source"
          position={Position.Bottom}
          style={{
            width: 8,
            height: 8,
            background: "#ef4444",
            border: "2px solid var(--handle-border)",
          }}
        />
      )}
    </div>
  );
}
