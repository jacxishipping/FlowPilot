"use client";

import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { FlowCanvasNode } from "@/lib/graphAdapter";

const CATEGORY_COLORS: Record<string, string> = {
  triggers: "#22d3ee",
  data: "#818cf8",
  control: "#f59e0b",
  http: "#38bdf8",
  ai: "#c084fc",
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
  const { nodeDef, catalog, executionStatus, durationMs, hasValidationError } = data;
  const category = catalog?.metadata.category ?? "core";
  const accent = CATEGORY_COLORS[category] ?? "#6366f1";
  const isTrigger = catalog?.metadata.kind === "trigger";
  const branches = catalog?.metadata.output_branches ?? [];

  return (
    <div
      className={`fp-node ${selected ? "selected" : ""}`}
      style={{
        borderColor: hasValidationError
          ? "#ef4444"
          : selected
            ? "#818cf8"
            : undefined,
      }}
    >
      {!isTrigger && (
        <Handle
          id="in"
          type="target"
          position={Position.Left}
          style={{ width: 10, height: 10, background: "#94a3b8", border: "2px solid #0b0e14" }}
        />
      )}

      <div className="fp-node-header" style={{ borderTop: `3px solid ${accent}` }}>
        <div style={{ minWidth: 0 }}>
          <div
            style={{
              fontWeight: 700,
              fontSize: 12,
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}
          >
            {nodeDef.name || catalog?.metadata.name || nodeDef.id}
          </div>
          <div style={{ fontSize: 10, color: "#6c7a96", fontFamily: "monospace" }}>
            {nodeDef.id} · {nodeDef.type}
          </div>
        </div>
        {executionStatus && (
          <span className={statusBadgeClass(executionStatus)}>{executionStatus}</span>
        )}
      </div>

      <div className="fp-node-body">
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4, alignItems: "center" }}>
          <span className="pill">{category}</span>
          {nodeDef.policy.require_approval && (
            <span className="pill pill-warning">approval</span>
          )}
          {Object.keys(nodeDef.credentials).length > 0 && (
            <span className="pill pill-info">cred</span>
          )}
          {hasValidationError && <span className="pill pill-danger">invalid</span>}
          {typeof durationMs === "number" && (
            <span style={{ marginLeft: "auto", fontSize: 10, color: "#6c7a96" }}>
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
              top: "35%",
              width: 10,
              height: 10,
              background: "#10b981",
              border: "2px solid #0b0e14",
            }}
          />
          <Handle
            id="false"
            type="source"
            position={Position.Right}
            style={{
              top: "68%",
              width: 10,
              height: 10,
              background: "#f59e0b",
              border: "2px solid #0b0e14",
            }}
          />
        </>
      ) : (
        <Handle
          id="default"
          type="source"
          position={Position.Right}
          style={{
            top: "45%",
            width: 10,
            height: 10,
            background: accent,
            border: "2px solid #0b0e14",
          }}
        />
      )}

      {!isTrigger && (
        <Handle
          id="error"
          type="source"
          position={Position.Bottom}
          style={{
            width: 9,
            height: 9,
            background: "#ef4444",
            border: "2px solid #0b0e14",
          }}
        />
      )}
    </div>
  );
}
