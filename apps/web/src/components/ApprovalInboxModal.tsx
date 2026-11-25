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
    <div className="modal-backdrop">
      <div className="modal-card" style={{ width: 640 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 700, fontSize: 15 }}>
            <Inbox size={16} color="#fbbf24" /> Human Approvals Inbox ({items.length})
          </div>
          <button className="btn" style={{ height: 28 }} onClick={onClose}>
            <X size={14} />
          </button>
        </div>

        <p style={{ color: "var(--text-secondary)", fontSize: 12 }}>
          Review pending workflow & AI agent approval gates across all runs, or generate signed
          one-click Approve/Reject links for Slack and email.
        </p>

        {items.length === 0 ? (
          <div style={{ color: "var(--text-muted)", padding: "20px 0" }}>
            No approvals in queue yet. Run a workflow with a Human Approval Gate to populate this
            inbox.
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 10, maxHeight: 400, overflowY: "auto" }}>
            {items.map((item) => (
              <div
                key={item.id}
                style={{
                  padding: 12,
                  borderRadius: 8,
                  border: "1px solid var(--border)",
                  background: "var(--bg-elevated)",
                }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <strong>{item.title}</strong>
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
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
                  node:{item.node_id} · risk:{item.risk_level} · run:{item.run_id.slice(0, 8)}
                </div>
                {item.decision === "pending" && (
                  <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
                    <button
                      className="btn btn-success"
                      onClick={() => onDecide(item.id, true, "Approved from Inbox")}
                    >
                      <CheckCircle2 size={12} /> Approve
                    </button>
                    <button
                      className="btn btn-danger"
                      onClick={() => onDecide(item.id, false, "Rejected from Inbox")}
                    >
                      <XCircle size={12} /> Reject
                    </button>
                    <button
                      className="btn"
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
                      marginTop: 8,
                      padding: 8,
                      borderRadius: 6,
                      background: "var(--bg-app)",
                      fontSize: 10,
                      fontFamily: "monospace",
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
