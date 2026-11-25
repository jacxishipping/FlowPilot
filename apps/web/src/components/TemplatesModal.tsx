"use client";

import { Copy, LayoutTemplate, X } from "lucide-react";
import type { TemplateSummary } from "@/lib/api";

interface TemplatesModalProps {
  open: boolean;
  templates: TemplateSummary[];
  onClose: () => void;
  onSelectTemplate: (template: TemplateSummary) => void;
}

export function TemplatesModal({
  open,
  templates,
  onClose,
  onSelectTemplate,
}: TemplatesModalProps) {
  if (!open) return null;

  return (
    <div className="modal-backdrop">
      <div className="modal-card" style={{ width: 660 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 700, fontSize: 15 }}>
            <LayoutTemplate size={16} color="#38bdf8" /> Curated Workflow Templates Gallery
          </div>
          <button className="btn" style={{ height: 28 }} onClick={onClose}>
            <X size={14} />
          </button>
        </div>

        <p style={{ color: "var(--text-secondary)", fontSize: 12 }}>
          Clone a vetted starter pipeline directly onto the canvas, customize credentials and
          policies, and publish when ready.
        </p>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
            gap: 10,
            maxHeight: 400,
            overflowY: "auto",
          }}
        >
          {templates.map((tpl) => (
            <div
              key={tpl.slug}
              style={{
                padding: 12,
                borderRadius: 8,
                border: "1px solid var(--border)",
                background: "var(--bg-elevated)",
                display: "flex",
                flexDirection: "column",
                justifyContent: "space-between",
                gap: 8,
              }}
            >
              <div>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 6 }}>
                  <strong style={{ fontSize: 13 }}>{tpl.name}</strong>
                  <span className="pill">{tpl.category}</span>
                </div>
                <p style={{ fontSize: 11, color: "var(--text-secondary)", margin: "6px 0" }}>
                  {tpl.description}
                </p>
                <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
                  <span className="pill pill-info">{tpl.node_count} nodes</span>
                  {tpl.tags.map((tag) => (
                    <span key={tag} className="pill">
                      {tag}
                    </span>
                  ))}
                </div>
              </div>
              <button
                className="btn btn-primary"
                style={{ justifyContent: "center" }}
                onClick={() => onSelectTemplate(tpl)}
              >
                <Copy size={12} /> Clone to Canvas
              </button>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
