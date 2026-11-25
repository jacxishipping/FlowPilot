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
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div className="modal-title">
            <Sparkles size={16} color="var(--brand-accent)" />
            <span>Generate Workflow Draft from Natural Language</span>
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
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
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
