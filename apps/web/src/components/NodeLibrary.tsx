"use client";

import { useMemo, useState } from "react";
import { Plus, Search } from "lucide-react";
import type { NodeCatalogEntry } from "@/lib/types";

interface NodeLibraryProps {
  catalog: NodeCatalogEntry[];
  onAddNode: (entry: NodeCatalogEntry) => void;
}

export function NodeLibrary({ catalog, onAddNode }: NodeLibraryProps) {
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
    <aside className="sidebar-left">
      <div style={{ padding: 12, borderBottom: "1px solid var(--border)" }}>
        <div style={{ fontWeight: 700, fontSize: 12, marginBottom: 8 }}>Node Library</div>
        <div style={{ position: "relative" }}>
          <Search
            size={13}
            style={{
              position: "absolute",
              left: 9,
              top: 9,
              color: "var(--text-muted)",
              pointerEvents: "none",
            }}
          />
          <input
            className="input"
            style={{ paddingLeft: 28 }}
            placeholder="Search nodes (HTTP, AI, approval...)"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
      </div>

      <div className="panel-scroll">
        {grouped.map(([category, items]) => (
          <div key={category}>
            <div className="section-title">{category}</div>
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
                  <strong style={{ fontSize: 12 }}>{entry.metadata.name}</strong>
                  <Plus size={13} color="#818cf8" />
                </div>
                <div
                  style={{
                    fontSize: 10,
                    color: "var(--text-muted)",
                    fontFamily: "monospace",
                    marginTop: 2,
                  }}
                >
                  {entry.metadata.type}
                </div>
                <div style={{ fontSize: 11, color: "var(--text-secondary)", marginTop: 4 }}>
                  {entry.metadata.description}
                </div>
              </div>
            ))}
          </div>
        ))}
        {grouped.length === 0 && (
          <div style={{ color: "var(--text-muted)", fontSize: 12, textAlign: "center", marginTop: 24 }}>
            No matching nodes.
          </div>
        )}
      </div>
    </aside>
  );
}
