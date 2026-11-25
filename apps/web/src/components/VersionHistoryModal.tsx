"use client";

import { GitCommitHorizontal, X } from "lucide-react";

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
  onClose: () => void;
}

export function VersionHistoryModal({
  open,
  versions,
  currentRevision,
  onClose,
}: VersionHistoryModalProps) {
  if (!open) return null;

  return (
    <div className="modal-backdrop">
      <div className="modal-card" style={{ width: 620 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 700, fontSize: 15 }}>
            <GitCommitHorizontal size={16} color="#34d399" /> Immutable Published Versions
          </div>
          <button className="btn" style={{ height: 28 }} onClick={onClose}>
            <X size={14} />
          </button>
        </div>

        <p style={{ color: "var(--text-secondary)", fontSize: 12 }}>
          Draft revision: <strong>r{currentRevision}</strong>. Every published run pins an immutable
          version snapshot and content SHA-256 hash.
        </p>

        {versions.length === 0 ? (
          <div style={{ color: "var(--text-muted)", padding: "18px 0" }}>
            No published versions yet. Click <strong>Publish</strong> in the toolbar to freeze v1.
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 8, maxHeight: 360, overflowY: "auto" }}>
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
                          {hashChanged ? "graph changed" : "layout-only / identical"}
                        </span>
                      )}
                    </div>
                    <span style={{ fontSize: 10, color: "var(--text-muted)" }}>
                      {new Date(ver.published_at).toLocaleString()}
                    </span>
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
