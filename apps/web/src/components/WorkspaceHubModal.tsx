"use client";

import { useEffect, useState } from "react";
import { BarChart3, BookOpen, Database, Plug, Plus, Search, Trash2, X } from "lucide-react";
import {
  type DataTableRowItem,
  type DataTableSummary,
  type KnowledgeDocSummary,
  type WorkspaceInsights,
  createDataTableApi,
  deleteTableRowApi,
  getInsightsApi,
  importOpenApiConnectorApi,
  ingestKnowledgeDocApi,
  insertTableRowApi,
  listDataTablesApi,
  listKnowledgeDocsApi,
  listTableRowsApi,
  searchKnowledgeApi,
} from "@/lib/api";

export type HubTab = "insights" | "tables" | "knowledge" | "openapi";

interface WorkspaceHubModalProps {
  open: boolean;
  initialTab?: HubTab;
  apiKey: string | null;
  onClose: () => void;
  onConnectorsImported: () => Promise<void>;
}

export function WorkspaceHubModal({
  open,
  initialTab = "insights",
  apiKey,
  onClose,
  onConnectorsImported,
}: WorkspaceHubModalProps) {
  const [tab, setTab] = useState<HubTab>(initialTab);
  const [insights, setInsights] = useState<WorkspaceInsights | null>(null);
  const [tables, setTables] = useState<DataTableSummary[]>([]);
  const [selectedTableId, setSelectedTableId] = useState<string | null>(null);
  const [rows, setRows] = useState<DataTableRowItem[]>([]);
  const [newTableName, setNewTableName] = useState("");
  const [newRowJson, setNewRowJson] = useState('{"customer": "Acme", "status": "active"}');

  const [docs, setDocs] = useState<KnowledgeDocSummary[]>([]);
  const [docTitle, setDocTitle] = useState("");
  const [docContent, setDocContent] = useState("");
  const [kbQuery, setKbQuery] = useState("");
  const [kbHits, setKbHits] = useState<
    Array<{ chunk_id: string; score: number; content: string; collection: string }>
  >([]);

  const [openApiPrefix, setOpenApiPrefix] = useState("custom_crm");
  const [openApiJson, setOpenApiJson] = useState(
    JSON.stringify(
      {
        openapi: "3.0.0",
        servers: [{ url: "https://api.example.com" }],
        paths: {
          "/v1/customers/{id}": {
            get: { operationId: "get_customer", summary: "Get Customer" },
          },
        },
      },
      null,
      2,
    ),
  );
  const [hubStatus, setHubStatus] = useState<string | null>(null);

  useEffect(() => {
    if (open) setTab(initialTab);
  }, [open, initialTab]);

  useEffect(() => {
    if (!open || !apiKey) return;
    setHubStatus(null);
    if (tab === "insights") {
      void getInsightsApi(apiKey).then(setInsights).catch(() => undefined);
    } else if (tab === "tables") {
      void listDataTablesApi(apiKey)
        .then((list) => {
          setTables(list);
          if (list.length > 0 && !selectedTableId) setSelectedTableId(list[0].id);
        })
        .catch(() => undefined);
    } else if (tab === "knowledge") {
      void listKnowledgeDocsApi(apiKey).then(setDocs).catch(() => undefined);
    }
  }, [open, tab, apiKey, selectedTableId]);

  useEffect(() => {
    if (!open || !apiKey || !selectedTableId || tab !== "tables") return;
    void listTableRowsApi(apiKey, selectedTableId).then(setRows).catch(() => undefined);
  }, [open, apiKey, selectedTableId, tab]);

  if (!open) return null;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card" style={{ width: 720 }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div className="modal-title">
            <span>Workspace Hub</span>
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

        <div className="tabs" style={{ marginTop: 10, marginBottom: 14 }}>
          <button
            type="button"
            className={`tab-btn ${tab === "insights" ? "active" : ""}`}
            onClick={() => setTab("insights")}
          >
            <BarChart3 size={12} style={{ marginRight: 5 }} /> Insights
          </button>
          <button
            type="button"
            className={`tab-btn ${tab === "tables" ? "active" : ""}`}
            onClick={() => setTab("tables")}
          >
            <Database size={12} style={{ marginRight: 5 }} /> Data Tables
          </button>
          <button
            type="button"
            className={`tab-btn ${tab === "knowledge" ? "active" : ""}`}
            onClick={() => setTab("knowledge")}
          >
            <BookOpen size={12} style={{ marginRight: 5 }} /> Knowledge / RAG
          </button>
          <button
            type="button"
            className={`tab-btn ${tab === "openapi" ? "active" : ""}`}
            onClick={() => setTab("openapi")}
          >
            <Plug size={12} style={{ marginRight: 5 }} /> OpenAPI Import
          </button>
        </div>

        {!apiKey ? (
          <div className="surface-card" style={{ textAlign: "center", color: "var(--text-muted)" }}>
            Connecting workspace session... click Save or Sign In first if offline.
          </div>
        ) : (
          <>
            {hubStatus && (
              <div
                style={{
                  marginBottom: 10,
                  padding: "8px 10px",
                  borderRadius: 8,
                  background: "var(--info-soft)",
                  border: "1px solid var(--info-border)",
                  color: "var(--info)",
                  fontSize: 12,
                }}
              >
                {hubStatus}
              </div>
            )}

            {tab === "insights" && (
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))",
                  gap: 10,
                }}
              >
                {[
                  { label: "Total Runs", value: insights?.total_runs ?? 0 },
                  { label: "Success Rate", value: `${insights?.success_rate_pct ?? 100}%` },
                  { label: "Avg / p95 ms", value: `${insights?.avg_duration_ms ?? 0} / ${insights?.p95_duration_ms ?? 0}` },
                  { label: "AI Tokens", value: (insights?.total_ai_tokens ?? 0).toLocaleString() },
                  { label: "AI Spend", value: `$${(insights?.total_ai_cost_usd ?? 0).toFixed(4)}` },
                  { label: "Est. Hours Saved", value: `${insights?.estimated_hours_saved ?? 0}h` },
                ].map((kpi) => (
                  <div key={kpi.label} className="surface-card">
                    <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{kpi.label}</div>
                    <div style={{ fontSize: 20, fontWeight: 700, marginTop: 4 }}>{kpi.value}</div>
                  </div>
                ))}
              </div>
            )}

            {tab === "tables" && (
              <div>
                <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
                  <input
                    className="input"
                    placeholder="New table name (e.g. leads)"
                    value={newTableName}
                    onChange={(e) => setNewTableName(e.target.value)}
                  />
                  <button
                    type="button"
                    className="btn btn-primary"
                    disabled={!newTableName.trim()}
                    onClick={async () => {
                      const created = await createDataTableApi(apiKey, newTableName.trim());
                      setTables((prev) => [created, ...prev]);
                      setSelectedTableId(created.id);
                      setNewTableName("");
                      setHubStatus(`Created table '${created.name}'`);
                    }}
                  >
                    <Plus size={13} /> Create Table
                  </button>
                </div>

                {tables.length > 0 && (
                  <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 12 }}>
                    {tables.map((t) => (
                      <button
                        key={t.id}
                        type="button"
                        className={`btn btn-sm ${selectedTableId === t.id ? "btn-primary" : ""}`}
                        onClick={() => setSelectedTableId(t.id)}
                      >
                        {t.name}
                      </button>
                    ))}
                  </div>
                )}

                {selectedTableId && (
                  <>
                    <div style={{ display: "flex", gap: 8, marginBottom: 10 }}>
                      <input
                        className="input"
                        style={{ fontFamily: "var(--font-mono), monospace", fontSize: 11.5 }}
                        value={newRowJson}
                        onChange={(e) => setNewRowJson(e.target.value)}
                      />
                      <button
                        type="button"
                        className="btn btn-success"
                        onClick={async () => {
                          try {
                            const row = await insertTableRowApi(
                              apiKey,
                              selectedTableId,
                              JSON.parse(newRowJson),
                            );
                            setRows((prev) => [row, ...prev]);
                          } catch (err) {
                            setHubStatus(
                              `Insert failed: ${err instanceof Error ? err.message : String(err)}`,
                            );
                          }
                        }}
                      >
                        Insert Row
                      </button>
                    </div>
                    <div style={{ display: "flex", flexDirection: "column", gap: 6, maxHeight: 240, overflowY: "auto" }}>
                      {rows.map((r) => (
                        <div
                          key={r.id}
                          className="surface-card"
                          style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, padding: 8 }}
                        >
                          <code style={{ fontSize: 11, wordBreak: "break-all" }}>
                            {JSON.stringify(r.data)}
                          </code>
                          <button
                            type="button"
                            className="btn btn-ghost btn-icon"
                            onClick={async () => {
                              await deleteTableRowApi(apiKey, selectedTableId, r.id);
                              setRows((prev) => prev.filter((x) => x.id !== r.id));
                            }}
                          >
                            <Trash2 size={13} />
                          </button>
                        </div>
                      ))}
                      {rows.length === 0 && (
                        <div style={{ color: "var(--text-muted)", fontSize: 12 }}>No rows yet.</div>
                      )}
                    </div>
                  </>
                )}
              </div>
            )}

            {tab === "knowledge" && (
              <div>
                <div className="field-group">
                  <label className="field-label">Ingest Document (Markdown, TXT, CSV)</label>
                  <input
                    className="input"
                    style={{ marginBottom: 6 }}
                    placeholder="Document title (e.g. Refund Policy)"
                    value={docTitle}
                    onChange={(e) => setDocTitle(e.target.value)}
                  />
                  <textarea
                    className="textarea"
                    rows={3}
                    placeholder="Paste document text to chunk & embed for RAG..."
                    value={docContent}
                    onChange={(e) => setDocContent(e.target.value)}
                  />
                  <button
                    type="button"
                    className="btn btn-primary"
                    style={{ marginTop: 6 }}
                    disabled={!docTitle.trim() || !docContent.trim()}
                    onClick={async () => {
                      const res = await ingestKnowledgeDocApi(apiKey, {
                        title: docTitle.trim(),
                        content: docContent.trim(),
                      });
                      setDocTitle("");
                      setDocContent("");
                      setDocs(await listKnowledgeDocsApi(apiKey));
                      setHubStatus(`Indexed '${res.title}' (${res.chunk_count} chunks)`);
                    }}
                  >
                    Index into Knowledge Base
                  </button>
                </div>

                <div className="field-group">
                  <label className="field-label">Test Semantic RAG Search ({docs.length} docs)</label>
                  <div style={{ display: "flex", gap: 8 }}>
                    <input
                      className="input"
                      placeholder="Ask a question..."
                      value={kbQuery}
                      onChange={(e) => setKbQuery(e.target.value)}
                    />
                    <button
                      type="button"
                      className="btn"
                      disabled={!kbQuery.trim()}
                      onClick={async () => {
                        const res = await searchKnowledgeApi(apiKey, kbQuery.trim());
                        setKbHits(res.chunks);
                      }}
                    >
                      <Search size={13} /> Search
                    </button>
                  </div>
                  {kbHits.length > 0 && (
                    <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 8, maxHeight: 160, overflowY: "auto" }}>
                      {kbHits.map((hit) => (
                        <div key={hit.chunk_id} className="surface-card" style={{ padding: 8, fontSize: 11.5 }}>
                          <span className="pill pill-info" style={{ marginRight: 6 }}>
                            score {hit.score}
                          </span>
                          {hit.content}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            )}

            {tab === "openapi" && (
              <div>
                <div className="field-group">
                  <label className="field-label">Connector Prefix</label>
                  <input
                    className="input"
                    value={openApiPrefix}
                    onChange={(e) => setOpenApiPrefix(e.target.value)}
                  />
                </div>
                <div className="field-group">
                  <label className="field-label">OpenAPI 3.x Spec JSON</label>
                  <textarea
                    className="textarea"
                    rows={8}
                    value={openApiJson}
                    onChange={(e) => setOpenApiJson(e.target.value)}
                  />
                </div>
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={async () => {
                    try {
                      const res = await importOpenApiConnectorApi(apiKey, {
                        prefix: openApiPrefix.trim(),
                        spec: JSON.parse(openApiJson),
                      });
                      await onConnectorsImported();
                      setHubStatus(
                        `Imported ${res.registered_count} node(s): ${res.node_types.join(", ")}`,
                      );
                    } catch (err) {
                      setHubStatus(
                        `Import failed: ${err instanceof Error ? err.message : String(err)}`,
                      );
                    }
                  }}
                >
                  <Plug size={13} /> Generate &amp; Register Nodes
                </button>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
