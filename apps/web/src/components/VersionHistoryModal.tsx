"use client";

import { useState } from "react";
import { GitCompare, GitCommitHorizontal, RotateCcw, X } from "lucide-react";
import type { WorkflowDiffResult } from "@/lib/api";

export interface VersionSummary {
  id: string;
  workflow_id: string;
  version_number: number;
  content_hash: string;
  changelog: string;
  published_at: string;
}

interface VersionHistoryModalProps {
  open: boolean;
  versions: VersionSummary[];
  currentRevision: number;
  diffResult: WorkflowDiffResult | null;
  onClose: () => void;
  onCompare: (fromVersion: number, toVersion: number) => Promise<void>;
  onRollback: (versionNumber: number) => Promise<void>;
}

export function VersionHistoryModal({
  open,
  versions,
  currentRevision,
  diffResult,
  onClose,
  onCompare,
  onRollback,
}: VersionHistoryModalProps) {
  const [fromVer, setFromVer] = useState(1);
  const [toVer, setToVer] = useState(2);

  if (!open) return null;

  return (
    <div className="modal-backdrop">
      <div className="modal-card" style={{ width: 660 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 700, fontSize: 15 }}>
            <GitCommitHorizontal size={16} color="#34d399" /> Version History, Diff & Rollback
          </div>
          <button className="btn" style={{ height: 28 }} onClick={onClose}>
            <X size={14} />
          </button>
        </div>

        <p style={{ color: "var(--text-secondary)", fontSize: 12 }}>
          Draft revision: <strong>r{currentRevision}</strong>. Compare any two versions to highlight
          changes directly on the canvas, or roll back in one click.
        </p>

        {versions.length >= 2 && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              padding: 10,
              borderRadius: 8,
              background: "var(--bg-app)",
              border: "1px solid var(--border)",
              marginBottom: 12,
            }}
          >
            <GitCompare size={14} color="#818cf8" />
            <span style={{ fontSize: 11 }}>Compare v</span>
            <input
              className="input"
              style={{ width: 58, height: 28 }}
              type="number"
              min={1}
              value={fromVer}
              onChange={(e) => setFromVer(Number(e.target.value) || 1)}
            />
            <span style={{ fontSize: 11 }}>to v</span>
            <input
              className="input"
              style={{ width: 58, height: 28 }}
              type="number"
              min={1}
              value={toVer}
              onChange={(e) => setToVer(Number(e.target.value) || 2)}
            />
            <button className="btn btn-primary" onClick={() => onCompare(fromVer, toVer)}>
              Highlight Diff on Canvas
            </button>
          </div>
        )}

        {diffResult && (
          <div
            style={{
              padding: 10,
              borderRadius: 8,
              border: "1px solid rgba(99, 102, 241, 0.45)",
              background: "rgba(99, 102, 241, 0.08)",
              marginBottom: 12,
              fontSize: 11,
            }}
          >
            <strong>
              Diff v{diffResult.from_version} -&gt; v{diffResult.to_version}:
            </strong>{" "}
            +{diffResult.added_nodes.length} added, ~{diffResult.modified_nodes.length} modified, -
            {diffResult.removed_nodes.length} removed nodes
          </div>
        )}

        {versions.length === 0 ? (
          <div style={{ color: "var(--text-muted)", padding: "18px 0" }}>
            No published versions yet. Click <strong>Publish</strong> in the toolbar to freeze v1.
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 8, maxHeight: 340, overflowY: "auto" }}>
            {versions.map((ver, idx) => {
              const prev = versions[idx + 1];
              const hashChanged = prev ? prev.content_hash !== ver.content_hash : true;
              return (
                <div
                  key={ver.id}
                  style={{
                    padding: 10,
                    borderRadius: 8,
                    border: "1px solid var(--border)",
                    background: "var(--bg-elevated)",
                  }}
                >
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <span className="pill pill-success">v{ver.version_number}</span>
                      <span style={{ fontFamily: "monospace", fontSize: 11, color: "#9aa7c2" }}>
                        sha256:{ver.content_hash.slice(0, 12)}
                      </span>
                      {prev && (
                        <span className={hashChanged ? "pill pill-info" : "pill"}>
                          {hashChanged ? "graph changed" : "layout-only"}
                        </span>
                      )}
                    </div>
                    <button
                      className="btn"
                      style={{ height: 26, fontSize: 11 }}
                      onClick={() => onRollback(ver.version_number)}
                    >
                      <RotateCcw size={11} /> Rollback
                    </button>
                  </div>
                  {ver.changelog && (
                    <div style={{ marginTop: 6, fontSize: 12, color: "var(--text-primary)" }}>
                      {ver.changelog}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
