"use client";

import { useState } from "react";
import { Bot, Send, Sparkles, X } from "lucide-react";

interface AssistantTurn {
  role: "user" | "assistant";
  content: string;
}

interface AiAssistantModalProps {
  open: boolean;
  loading: boolean;
  onClose: () => void;
  onSend: (message: string) => Promise<string>;
}

export function AiAssistantModal({ open, loading, onClose, onSend }: AiAssistantModalProps) {
  const [input, setInput] = useState("");
  const [turns, setTurns] = useState<AssistantTurn[]>([
    {
      role: "assistant",
      content:
        "I can inspect, edit, and debug your live workflow canvas. Try: \"Add a Switch router and Slack notification\" or \"Add a RAG knowledge search step\".",
    },
  ]);

  if (!open) return null;

  const submit = async () => {
    const text = input.trim();
    if (!text || loading) return;
    setInput("");
    setTurns((prev) => [...prev, { role: "user", content: text }]);
    try {
      const reply = await onSend(text);
      setTurns((prev) => [...prev, { role: "assistant", content: reply }]);
    } catch (err) {
      setTurns((prev) => [
        ...prev,
        {
          role: "assistant",
          content: `Error: ${err instanceof Error ? err.message : String(err)}`,
        },
      ]);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card" style={{ width: 560 }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div className="modal-title">
            <Bot size={16} color="var(--brand-accent)" />
            <span>FlowPilot AI Assistant</span>
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
          Chat to modify nodes, add integrations, or diagnose validation issues on your live canvas.
        </p>

        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 8,
            maxHeight: 300,
            overflowY: "auto",
            marginBottom: 12,
          }}
        >
          {turns.map((t, i) => (
            <div
              key={i}
              className="surface-card"
              style={{
                alignSelf: t.role === "user" ? "flex-end" : "flex-start",
                maxWidth: "88%",
                padding: "8px 11px",
                background: t.role === "user" ? "var(--accent)" : "var(--bg-elevated)",
                color: t.role === "user" ? "var(--accent-fg)" : "var(--text-primary)",
                fontSize: 12.5,
              }}
            >
              {t.content}
            </div>
          ))}
        </div>

        <div style={{ display: "flex", gap: 8 }}>
          <input
            className="input"
            placeholder="e.g. Add a Slack alert and Human Approval Gate..."
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void submit();
            }}
          />
          <button
            type="button"
            className="btn btn-primary"
            disabled={loading || !input.trim()}
            onClick={() => void submit()}
          >
            {loading ? <Sparkles size={13} /> : <Send size={13} />} Send
          </button>
        </div>
      </div>
    </div>
  );
}
