"use client";

import { useState } from "react";
import { CheckCircle2, Inbox, Link2, X, XCircle } from "lucide-react";
import type { InboxApprovalItem } from "@/lib/api";

interface ApprovalInboxModalProps {
  open: boolean;
  items: InboxApprovalItem[];
  onClose: () => void;
  onDecide: (approvalId: string, approved: boolean, comment: string) => Promise<void>;
  onGenerateLinks: (approvalId: string) => Promise<{ approve_url: string; reject_url: string }>;
}

export function ApprovalInboxModal({
  open,
  items,
  onClose,
  onDecide,
  onGenerateLinks,
}: ApprovalInboxModalProps) {
  const [linksById, setLinksById] = useState<
    Record<string, { approve_url: string; reject_url: string }>
  >({});

  if (!open) return null;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card" style={{ width: 620 }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div className="modal-title">
            <Inbox size={16} color="var(--warning)" />
            <span>Human Approvals Inbox ({items.length})</span>
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
          Review pending workflow &amp; AI agent approval gates across runs, or generate signed
          one-click Approve/Reject links for Slack and email.
        </p>

        {items.length === 0 ? (
          <div
            className="surface-card"
            style={{ color: "var(--text-muted)", textAlign: "center", padding: "24px 16px" }}
          >
            No approvals in queue yet. Run a workflow with a Human Approval Gate to populate this
            inbox.
          </div>
        ) : (
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 10,
              maxHeight: "58dvh",
              overflowY: "auto",
            }}
          >
            {items.map((item) => (
              <div key={item.id} className="surface-card">
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
                  <strong style={{ fontSize: 13 }}>{item.title}</strong>
                  <span
                    className={
                      item.decision === "pending"
                        ? "pill pill-warning"
                        : item.decision === "approved"
                          ? "pill pill-success"
                          : "pill pill-danger"
                    }
                  >
                    {item.decision}
                  </span>
                </div>
                <div
                  style={{
                    fontSize: 11,
                    color: "var(--text-muted)",
                    fontFamily: "var(--font-mono), monospace",
                    marginTop: 4,
                  }}
                >
                  node:{item.node_id} · risk:{item.risk_level} · run:{item.run_id.slice(0, 8)}
                </div>
                {item.decision === "pending" && (
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 10 }}>
                    <button
                      type="button"
                      className="btn btn-success btn-sm"
                      onClick={() => onDecide(item.id, true, "Approved from Inbox")}
                    >
                      <CheckCircle2 size={12} /> Approve
                    </button>
                    <button
                      type="button"
                      className="btn btn-danger btn-sm"
                      onClick={() => onDecide(item.id, false, "Rejected from Inbox")}
                    >
                      <XCircle size={12} /> Reject
                    </button>
                    <button
                      type="button"
                      className="btn btn-sm"
                      onClick={async () => {
                        const res = await onGenerateLinks(item.id);
                        setLinksById((prev) => ({ ...prev, [item.id]: res }));
                      }}
                    >
                      <Link2 size={12} /> Signed Slack/Email Links
                    </button>
                  </div>
                )}
                {linksById[item.id] && (
                  <div
                    style={{
                      marginTop: 10,
                      padding: 10,
                      borderRadius: 6,
                      background: "var(--bg-panel)",
                      border: "1px solid var(--border)",
                      fontSize: 10.5,
                      fontFamily: "var(--font-mono), monospace",
                      wordBreak: "break-all",
                    }}
                  >
                    <div>Approve: {linksById[item.id].approve_url}</div>
                    <div style={{ marginTop: 4 }}>Reject: {linksById[item.id].reject_url}</div>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
