"use client";

import { useMemo, useState } from "react";
import { Plus, Search, X } from "lucide-react";
import type { NodeCatalogEntry } from "@/lib/types";

const CATEGORY_DOT: Record<string, string> = {
  triggers: "#06b6d4",
  data: "#6366f1",
  control: "#f59e0b",
  http: "#3b82f6",
  ai: "#a855f7",
  connectors: "#10b981",
  communication: "#ec4899",
};

interface NodeLibraryProps {
  catalog: NodeCatalogEntry[];
  open?: boolean;
  onClose?: () => void;
  onAddNode: (entry: NodeCatalogEntry) => void;
}

export function NodeLibrary({ catalog, open = true, onClose, onAddNode }: NodeLibraryProps) {
  const [query, setQuery] = useState("");

  const grouped = useMemo(() => {
    const q = query.trim().toLowerCase();
    const filtered = catalog.filter((entry) => {
      if (!q) return true;
      return (
        entry.metadata.name.toLowerCase().includes(q) ||
        entry.metadata.type.toLowerCase().includes(q) ||
        entry.metadata.category.toLowerCase().includes(q) ||
        entry.metadata.description.toLowerCase().includes(q)
      );
    });

    const map = new Map<string, NodeCatalogEntry[]>();
    for (const item of filtered) {
      const cat = item.metadata.category;
      const list = map.get(cat) ?? [];
      list.push(item);
      map.set(cat, list);
    }
    return Array.from(map.entries());
  }, [catalog, query]);

  return (
    <aside className={`sidebar-left ${open ? "" : "collapsed"}`} aria-label="Node Library">
      <div style={{ padding: "12px 14px", borderBottom: "1px solid var(--border)" }}>
        <div className="panel-header-row">
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{ fontWeight: 600, fontSize: 12.5 }}>Node Library</span>
            <span className="pill">{catalog.length}</span>
          </div>
          {onClose && (
            <button
              type="button"
              className="btn btn-ghost btn-icon panel-close-btn"
              onClick={onClose}
              aria-label="Close Node Library"
            >
              <X size={15} />
            </button>
          )}
        </div>
        <div style={{ position: "relative" }}>
          <Search
            size={13}
            style={{
              position: "absolute",
              left: 10,
              top: "50%",
              transform: "translateY(-50%)",
              color: "var(--text-muted)",
              pointerEvents: "none",
            }}
          />
          <input
            className="input"
            style={{ paddingLeft: 30, height: 32 }}
            placeholder="Search nodes..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
      </div>

      <div className="panel-scroll">
        {grouped.map(([category, items]) => (
          <div key={category} style={{ marginBottom: 10 }}>
            <div className="section-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <span
                style={{
                  width: 6,
                  height: 6,
                  borderRadius: 999,
                  background: CATEGORY_DOT[category] ?? "var(--text-muted)",
                }}
              />
              {category}
            </div>
            {items.map((entry) => (
              <div
                key={entry.metadata.type}
                className="node-palette-card"
                draggable
                onDragStart={(ev) => {
                  ev.dataTransfer.setData("application/flowpilot-node", entry.metadata.type);
                  ev.dataTransfer.effectAllowed = "move";
                }}
                onClick={() => onAddNode(entry)}
                title="Drag onto canvas or click to add"
              >
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: 6,
                  }}
                >
                  <span style={{ fontSize: 12.5, fontWeight: 500, color: "var(--text-primary)" }}>
                    {entry.metadata.name}
                  </span>
                  <Plus size={13} color="var(--text-muted)" />
                </div>
                <div
                  style={{
                    fontSize: 10.5,
                    color: "var(--text-muted)",
                    fontFamily: "var(--font-mono), monospace",
                    marginTop: 1,
                  }}
                >
                  {entry.metadata.type}
                </div>
                <div
                  style={{
                    fontSize: 11.5,
                    color: "var(--text-secondary)",
                    marginTop: 3,
                    display: "-webkit-box",
                    WebkitLineClamp: 2,
                    WebkitBoxOrient: "vertical",
                    overflow: "hidden",
                  }}
                >
                  {entry.metadata.description}
                </div>
              </div>
            ))}
          </div>
        ))}
        {grouped.length === 0 && (
          <div style={{ color: "var(--text-muted)", fontSize: 12, textAlign: "center", marginTop: 28 }}>
            No matching nodes.
          </div>
        )}
      </div>
    </aside>
  );
}
