"use client";

import { useState } from "react";
import { Sparkles, X } from "lucide-react";

interface AiDraftModalProps {
  open: boolean;
  loading: boolean;
  onClose: () => void;
  onGenerate: (prompt: string) => Promise<void>;
}

export function AiDraftModal({ open, loading, onClose, onGenerate }: AiDraftModalProps) {
  const [prompt, setPrompt] = useState(
    "When a webhook arrives, fetch the customer from our CRM API, triage with a bounded AI agent, and require human approval before executing.",
  );

  if (!open) return null;

  return (
    <div className="modal-backdrop">
      <div className="modal-card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 700, fontSize: 15 }}>
            <Sparkles size={16} color="#c084fc" /> Generate Workflow Draft from Natural Language
          </div>
          <button className="btn" style={{ height: 28 }} onClick={onClose}>
            <X size={14} />
          </button>
        </div>

        <p style={{ color: "var(--text-secondary)", fontSize: 12, lineHeight: 1.5 }}>
          FlowPilot generates a typed canonical JSON workflow graph matched against installed nodes
          and validates it immediately. Generated drafts are never auto-published or auto-run.
        </p>

        <textarea
          className="textarea"
          rows={5}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="Describe triggers, integrations, AI reasoning, and approval gates..."
        />

        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 14 }}>
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn btn-primary"
            disabled={loading || prompt.trim().length < 5}
            onClick={() => onGenerate(prompt.trim())}
          >
            <Sparkles size={13} /> {loading ? "Generating..." : "Generate Editable Draft"}
          </button>
        </div>
      </div>
    </div>
  );
}
