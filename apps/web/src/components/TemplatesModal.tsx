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
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card" style={{ width: 680 }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div className="modal-title">
            <LayoutTemplate size={16} color="var(--brand-accent)" />
            <span>Curated Workflow Templates Gallery</span>
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
          Clone a vetted starter pipeline directly onto the canvas, customize credentials and
          policies, and publish when ready.
        </p>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))",
            gap: 10,
            maxHeight: "60dvh",
            overflowY: "auto",
          }}
        >
          {templates.map((tpl) => (
            <div
              key={tpl.slug}
              className="surface-card"
              style={{
                display: "flex",
                flexDirection: "column",
                justifyContent: "space-between",
                gap: 10,
              }}
            >
              <div>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 6 }}>
                  <strong style={{ fontSize: 13 }}>{tpl.name}</strong>
                  <span className="pill">{tpl.category}</span>
                </div>
                <p style={{ fontSize: 12, color: "var(--text-secondary)", margin: "6px 0 8px" }}>
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
                type="button"
                className="btn btn-primary"
                style={{ width: "100%" }}
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
