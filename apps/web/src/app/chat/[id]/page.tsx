"use client";

import { use, useState } from "react";
import { ArrowLeft, Bot, Send } from "lucide-react";
import Link from "next/link";
import { bootstrapDevTenant, sendChatMessageApi } from "@/lib/api";

interface MessageItem {
  role: "user" | "assistant";
  content: string;
}

export default function HostedChatPage({ params }: { params: Promise<{ id: string }> }) {
  const { id: workflowId } = use(params);
  const [messages, setMessages] = useState<MessageItem[]>([
    {
      role: "assistant",
      content: "Connected to FlowPilot ChatHub. Send a message to trigger this workflow.",
    },
  ]);
  const [input, setInput] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [apiKey, setApiKey] = useState<string | null>(null);
  const [sending, setSending] = useState(false);

  const handleSend = async () => {
    const text = input.trim();
    if (!text || sending) return;
    setInput("");
    setSending(true);
    setMessages((prev) => [...prev, { role: "user", content: text }]);
    try {
      let key = apiKey;
      if (!key) {
        const sess = await bootstrapDevTenant(`chat-${Date.now().toString(36)}`);
        key = sess.api_key;
        setApiKey(key);
      }
      const res = await sendChatMessageApi(key, workflowId, text, sessionId);
      setSessionId(res.session_id);
      setMessages((prev) => [...prev, { role: "assistant", content: res.reply }]);
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content: `Could not run workflow: ${err instanceof Error ? err.message : String(err)}`,
        },
      ]);
    } finally {
      setSending(false);
    }
  };

  return (
    <div
      style={{
        minHeight: "100dvh",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        background: "var(--bg-app)",
        padding: 16,
      }}
    >
      <div
        style={{
          width: "100%",
          maxWidth: 720,
          flex: 1,
          display: "flex",
          flexDirection: "column",
          background: "var(--bg-panel)",
          border: "1px solid var(--border)",
          borderRadius: 14,
          overflow: "hidden",
          boxShadow: "var(--shadow-sm)",
        }}
      >
        <header
          style={{
            padding: "12px 16px",
            borderBottom: "1px solid var(--border)",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 600 }}>
            <Bot size={16} color="var(--brand-accent)" />
            <span>FlowPilot ChatHub</span>
            <span className="pill">{workflowId.slice(0, 8)}</span>
          </div>
          <Link href="/" className="btn btn-ghost btn-sm" style={{ textDecoration: "none" }}>
            <ArrowLeft size={13} /> Studio
          </Link>
        </header>

        <div
          style={{
            flex: 1,
            padding: 16,
            display: "flex",
            flexDirection: "column",
            gap: 10,
            overflowY: "auto",
          }}
        >
          {messages.map((m, idx) => (
            <div
              key={idx}
              style={{
                alignSelf: m.role === "user" ? "flex-end" : "flex-start",
                maxWidth: "82%",
                padding: "10px 13px",
                borderRadius: 12,
                background: m.role === "user" ? "var(--accent)" : "var(--bg-elevated)",
                color: m.role === "user" ? "var(--accent-fg)" : "var(--text-primary)",
                border: m.role === "user" ? "none" : "1px solid var(--border)",
                fontSize: 13,
                lineHeight: 1.5,
                whiteSpace: "pre-wrap",
              }}
            >
              {m.content}
            </div>
          ))}
        </div>

        <div
          style={{
            padding: 12,
            borderTop: "1px solid var(--border)",
            display: "flex",
            gap: 8,
          }}
        >
          <input
            className="input"
            placeholder="Message this workflow..."
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void handleSend();
            }}
          />
          <button
            type="button"
            className="btn btn-primary"
            disabled={sending || !input.trim()}
            onClick={() => void handleSend()}
          >
            <Send size={13} /> Send
          </button>
        </div>
      </div>
    </div>
  );
}
