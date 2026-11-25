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
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card" style={{ width: 640 }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div className="modal-title">
            <GitCommitHorizontal size={16} color="var(--success)" />
            <span>Version History, Diff &amp; Rollback</span>
          </div>
          <button
            type="button"
            className="btn btn-ghost btn-icon"
            onClick={onClose}
            aria-label="Close"
          >
            <X size={15} />
          </button>
        </div>

        <p className="modal-subtitle">
          Draft revision: <strong>r{currentRevision}</strong>. Compare any two published versions to
          highlight diffs on the canvas, or roll back in one click.
        </p>

        {versions.length >= 2 && (
          <div
            className="surface-card"
            style={{
              display: "flex",
              flexWrap: "wrap",
              alignItems: "center",
              gap: 8,
              marginBottom: 12,
            }}
          >
            <GitCompare size={14} color="var(--brand-accent)" />
            <span style={{ fontSize: 12 }}>Compare v</span>
            <input
              className="input"
              style={{ width: 60, height: 28 }}
              type="number"
              min={1}
              value={fromVer}
              onChange={(e) => setFromVer(Number(e.target.value) || 1)}
            />
            <span style={{ fontSize: 12 }}>to v</span>
            <input
              className="input"
              style={{ width: 60, height: 28 }}
              type="number"
              min={1}
              value={toVer}
              onChange={(e) => setToVer(Number(e.target.value) || 2)}
            />
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={() => onCompare(fromVer, toVer)}
            >
              Highlight Diff
            </button>
          </div>
        )}

        {diffResult && (
          <div
            style={{
              padding: 10,
              borderRadius: 8,
              border: "1px solid var(--info-border)",
              background: "var(--info-soft)",
              color: "var(--info)",
              marginBottom: 12,
              fontSize: 12,
            }}
          >
            <strong>
              Diff v{diffResult.from_version} -&gt; v{diffResult.to_version}:
            </strong>{" "}
            +{diffResult.added_nodes.length} added, ~{diffResult.modified_nodes.length} modified, -
            {diffResult.removed_nodes.length} removed
          </div>
        )}

        {versions.length === 0 ? (
          <div
            className="surface-card"
            style={{ color: "var(--text-muted)", textAlign: "center", padding: "24px 16px" }}
          >
            No published versions yet. Click <strong>Publish</strong> in the toolbar to freeze v1.
          </div>
        ) : (
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 8,
              maxHeight: "52dvh",
              overflowY: "auto",
            }}
          >
            {versions.map((ver, idx) => {
              const prev = versions[idx + 1];
              const hashChanged = prev ? prev.content_hash !== ver.content_hash : true;
              return (
                <div key={ver.id} className="surface-card">
                  <div style={{ display: "flex", flexWrap: "wrap", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                      <span className="pill pill-success">v{ver.version_number}</span>
                      <span
                        style={{
                          fontFamily: "var(--font-mono), monospace",
                          fontSize: 11,
                          color: "var(--text-muted)",
                        }}
                      >
                        sha256:{ver.content_hash.slice(0, 12)}
                      </span>
                      {prev && (
                        <span className={hashChanged ? "pill pill-info" : "pill"}>
                          {hashChanged ? "graph changed" : "layout-only"}
                        </span>
                      )}
                    </div>
                    <button
                      type="button"
                      className="btn btn-sm"
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
