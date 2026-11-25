"use client";

import { useMemo, useState } from "react";
import { CheckCircle2, KeyRound, Play, ShieldAlert, Sparkles, Trash2, XCircle } from "lucide-react";
import { previewExpressionApi } from "@/lib/api";
import type {
  CredentialMeta,
  EdgeDefinition,
  EdgeKind,
  ExecutionEvent,
  NodeCatalogEntry,
  NodeDefinition,
  ValidationResult,
  WorkflowRunState,
} from "@/lib/types";

export type InspectorTab = "configure" | "validate" | "runs" | "credentials";

interface InspectorPanelProps {
  activeTab: InspectorTab;
  onTabChange: (tab: InspectorTab) => void;
  selectedNode: NodeDefinition | null;
  selectedEdge: EdgeDefinition | null;
  upstreamNodeIds: string[];
  catalogEntry?: NodeCatalogEntry;
  validation: ValidationResult | null;
  workflowJsonSchema: Record<string, unknown> | null;
  activeRun: WorkflowRunState | null;
  runEvents: ExecutionEvent[];
  credentials: CredentialMeta[];
  triggerInputJson: string;
  onTriggerInputChange: (val: string) => void;
  onUpdateNode: (nodeId: string, updater: (node: NodeDefinition) => NodeDefinition) => void;
  onDeleteNode: (nodeId: string) => void;
  onUpdateEdge: (edgeId: string, kind: EdgeKind) => void;
  onDeleteEdge: (edgeId: string) => void;
  onSelectNodeId: (nodeId: string) => void;
  onRunWorkflow: () => void;
  onReplayFromNode: (nodeId: string) => void;
  pinnedOutputs: Record<string, unknown>;
  onPinNodeOutput: (nodeId: string, output: unknown) => void;
  onDecideApproval: (approvalId: string, approved: boolean, comment: string) => void;
  onCreateCredential: (name: string, type: string, token: string) => Promise<void>;
  connected: boolean;
}

export function InspectorPanel({
  activeTab,
  onTabChange,
  selectedNode,
  selectedEdge,
  upstreamNodeIds,
  catalogEntry,
  validation,
  workflowJsonSchema,
  activeRun,
  runEvents,
  credentials,
  triggerInputJson,
  onTriggerInputChange,
  onUpdateNode,
  onDeleteNode,
  onUpdateEdge,
  onDeleteEdge,
  onSelectNodeId,
  onRunWorkflow,
  onReplayFromNode,
  pinnedOutputs,
  onPinNodeOutput,
  onDecideApproval,
  onCreateCredential,
  connected,
}: InspectorPanelProps) {
  const [credName, setCredName] = useState("");
  const [credType, setCredType] = useState("http_auth");
  const [credSecret, setCredSecret] = useState("");
  const [approvalComment, setApprovalComment] = useState("Verified in FlowPilot Studio");
  const [showSchemaJson, setShowSchemaJson] = useState(false);

  const errorCount = validation?.issues.filter((i) => i.severity === "error").length ?? 0;

  return (
    <aside className="sidebar-right">
      <div className="tabs">
        <button
          className={`tab-btn ${activeTab === "configure" ? "active" : ""}`}
          onClick={() => onTabChange("configure")}
        >
          Configure
        </button>
        <button
          className={`tab-btn ${activeTab === "validate" ? "active" : ""}`}
          onClick={() => onTabChange("validate")}
        >
          Validate {errorCount > 0 ? `(${errorCount})` : ""}
        </button>
        <button
          className={`tab-btn ${activeTab === "runs" ? "active" : ""}`}
          onClick={() => onTabChange("runs")}
        >
          Runs
        </button>
        <button
          className={`tab-btn ${activeTab === "credentials" ? "active" : ""}`}
          onClick={() => onTabChange("credentials")}
        >
          Vault
        </button>
      </div>

      <div className="panel-scroll">
        {activeTab === "configure" && (
          <>
            {selectedNode ? (
              <NodeConfigEditor
                node={selectedNode}
                catalogEntry={catalogEntry}
                upstreamNodeIds={upstreamNodeIds}
                pinnedOutputs={pinnedOutputs}
                triggerInputJson={triggerInputJson}
                credentials={credentials}
                onUpdate={(updater) => onUpdateNode(selectedNode.id, updater)}
                onDelete={() => onDeleteNode(selectedNode.id)}
              />
            ) : selectedEdge ? (
              <div>
                <div className="section-title">Selected Edge</div>
                <div style={{ fontFamily: "monospace", marginBottom: 10 }}>
                  {selectedEdge.source} -&gt; {selectedEdge.target}
                </div>
                <div className="field-group">
                  <label className="field-label">Edge Routing Kind</label>
                  <select
                    className="select"
                    value={selectedEdge.kind}
                    onChange={(e) => onUpdateEdge(selectedEdge.id, e.target.value as EdgeKind)}
                  >
                    <option value="default">default (standard flow)</option>
                    <option value="true">true (condition / approved branch)</option>
                    <option value="false">false (else / rejected branch)</option>
                    <option value="error">error (catch upstream node failure)</option>
                  </select>
                </div>
                <button className="btn btn-danger" onClick={() => onDeleteEdge(selectedEdge.id)}>
                  <Trash2 size={13} /> Delete Edge
                </button>
              </div>
            ) : (
              <div style={{ color: "var(--text-muted)", marginTop: 20, lineHeight: 1.5 }}>
                Select a node or edge on the canvas to configure typed schema fields, expressions,
                credentials, retries, and approval gates.
              </div>
            )}
          </>
        )}

        {activeTab === "validate" && (
          <div>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <span className="section-title" style={{ margin: 0 }}>
                Pre-Publish Diagnostics
              </span>
              <button
                className="btn"
                style={{ height: 26, fontSize: 11 }}
                onClick={() => setShowSchemaJson((v) => !v)}
              >
                {showSchemaJson ? "Hide Schema" : "Canonical JSON Schema"}
              </button>
            </div>

            {showSchemaJson && (
              <pre
                style={{
                  background: "var(--bg-app)",
                  border: "1px solid var(--border)",
                  borderRadius: 8,
                  padding: 10,
                  fontSize: 10,
                  maxHeight: 220,
                  overflow: "auto",
                  marginTop: 10,
                }}
              >
                {JSON.stringify(workflowJsonSchema ?? { note: "Connect API to load live JSON Schema" }, null, 2)}
              </pre>
            )}

            {!validation ? (
              <div style={{ color: "var(--text-muted)", marginTop: 14 }}>
                Click <strong>Validate</strong> in the top toolbar to run DAG, expression,
                schema, credential, and side-effect checks.
              </div>
            ) : validation.issues.length === 0 ? (
              <div
                style={{
                  marginTop: 14,
                  padding: 12,
                  borderRadius: 8,
                  border: "1px solid rgba(16, 185, 129, 0.35)",
                  background: "rgba(16, 185, 129, 0.1)",
                  color: "#34d399",
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                }}
              >
                <CheckCircle2 size={16} /> Workflow passed all pre-publish validation checks.
              </div>
            ) : (
              <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 8 }}>
                {validation.issues.map((issue, idx) => (
                  <div
                    key={`${issue.code}-${idx}`}
                    onClick={() => issue.node_id && onSelectNodeId(issue.node_id)}
                    style={{
                      padding: 10,
                      borderRadius: 8,
                      border: `1px solid ${
                        issue.severity === "error"
                          ? "rgba(239,68,68,0.4)"
                          : "rgba(245,158,11,0.4)"
                      }`,
                      background: "var(--bg-elevated)",
                      cursor: issue.node_id ? "pointer" : "default",
                    }}
                  >
                    <div style={{ display: "flex", justifyContent: "space-between", gap: 6 }}>
                      <span
                        className={
                          issue.severity === "error" ? "pill pill-danger" : "pill pill-warning"
                        }
                      >
                        {issue.code}
                      </span>
                      {issue.node_id && (
                        <span style={{ fontFamily: "monospace", fontSize: 10, color: "#9aa7c2" }}>
                          node:{issue.node_id}
                        </span>
                      )}
                    </div>
                    <div style={{ marginTop: 6, fontSize: 11, color: "var(--text-primary)" }}>
                      {issue.message}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {activeTab === "runs" && (
          <div>
            <div className="field-group">
              <label className="field-label">Trigger Input JSON</label>
              <textarea
                className="textarea"
                rows={4}
                value={triggerInputJson}
                onChange={(e) => onTriggerInputChange(e.target.value)}
              />
            </div>
            <button
              className="btn btn-success"
              style={{ width: "100%", justifyContent: "center" }}
              onClick={onRunWorkflow}
              disabled={!connected}
            >
              <Play size={13} /> Execute Published Version
            </button>

            {activeRun && (
              <div style={{ marginTop: 14 }}>
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    marginBottom: 8,
                  }}
                >
                  <strong style={{ fontSize: 12 }}>Active Run</strong>
                  <span
                    className={
                      activeRun.status === "SUCCEEDED"
                        ? "pill pill-success"
                        : activeRun.status === "WAITING_APPROVAL"
                          ? "pill pill-warning"
                          : activeRun.status === "FAILED"
                            ? "pill pill-danger"
                            : "pill pill-info"
                    }
                  >
                    {activeRun.status}
                  </span>
                </div>

                {/* Human Approval Gate Cards */}
                {Object.values(activeRun.approvals)
                  .filter((a) => a.decision === "pending")
                  .map((approval) => (
                    <div
                      key={approval.id}
                      style={{
                        padding: 12,
                        borderRadius: 8,
                        border: "1px solid rgba(245, 158, 11, 0.5)",
                        background: "rgba(245, 158, 11, 0.08)",
                        marginBottom: 12,
                      }}
                    >
                      <div style={{ display: "flex", alignItems: "center", gap: 6, fontWeight: 700 }}>
                        <ShieldAlert size={14} color="#fbbf24" /> {approval.title}
                      </div>
                      {approval.description && (
                        <div style={{ fontSize: 11, color: "var(--text-secondary)", marginTop: 4 }}>
                          {approval.description}
                        </div>
                      )}
                      <input
                        className="input"
                        style={{ marginTop: 8 }}
                        value={approvalComment}
                        onChange={(e) => setApprovalComment(e.target.value)}
                        placeholder="Decision comment..."
                      />
                      <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
                        <button
                          className="btn btn-success"
                          style={{ flex: 1, justifyContent: "center" }}
                          onClick={() => onDecideApproval(approval.id, true, approvalComment)}
                        >
                          <CheckCircle2 size={13} /> Approve
                        </button>
                        <button
                          className="btn btn-danger"
                          style={{ flex: 1, justifyContent: "center" }}
                          onClick={() => onDecideApproval(approval.id, false, approvalComment)}
                        >
                          <XCircle size={13} /> Reject
                        </button>
                      </div>
                    </div>
                  ))}

                <div className="section-title">Node Outputs, Pinning & Time-Travel Replay</div>
                <div style={{ display: "flex", flexDirection: "column", gap: 6, marginBottom: 12 }}>
                  {Object.values(activeRun.node_executions).map((ne) => (
                    <div
                      key={ne.node_id}
                      style={{
                        padding: 8,
                        borderRadius: 6,
                        border: "1px solid var(--border)",
                        background: "var(--bg-elevated)",
                        fontSize: 11,
                      }}
                    >
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                        <span style={{ fontFamily: "monospace", fontWeight: 700 }}>{ne.node_id}</span>
                        <span className="pill">{ne.status}</span>
                      </div>
                      <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
                        <button
                          className="btn"
                          style={{ height: 24, fontSize: 10 }}
                          onClick={() => onReplayFromNode(ne.node_id)}
                        >
                          Fork & Replay Here
                        </button>
                        {ne.output_ref?.inline !== undefined && (
                          <button
                            className="btn"
                            style={{ height: 24, fontSize: 10 }}
                            onClick={() => onPinNodeOutput(ne.node_id, ne.output_ref?.inline)}
                          >
                            {pinnedOutputs[ne.node_id] !== undefined ? "Pinned ✓" : "Pin Output"}
                          </button>
                        )}
                      </div>
                    </div>
                  ))}
                </div>

                <div className="section-title">SSE Execution Event Log ({runEvents.length})</div>
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  {runEvents.map((evt) => (
                    <div
                      key={evt.id}
                      style={{
                        padding: "6px 8px",
                        borderRadius: 6,
                        background: "var(--bg-app)",
                        border: "1px solid var(--border)",
                        fontSize: 11,
                        fontFamily: "monospace",
                      }}
                    >
                      <span style={{ color: "#6c7a96" }}>#{evt.sequence}</span>{" "}
                      <strong style={{ color: "#818cf8" }}>{evt.event_type}</strong>
                      {evt.node_id && <span style={{ color: "#34d399" }}> [{evt.node_id}]</span>}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {activeTab === "credentials" && (
          <div>
            <div className="section-title">Envelope-Encrypted Vault (AES-256-GCM)</div>
            <div className="field-group">
              <label className="field-label">Credential Name</label>
              <input
                className="input"
                value={credName}
                onChange={(e) => setCredName(e.target.value)}
                placeholder="e.g. Stripe Production Key"
              />
            </div>
            <div className="field-group">
              <label className="field-label">Type</label>
              <select
                className="select"
                value={credType}
                onChange={(e) => setCredType(e.target.value)}
              >
                <option value="http_auth">http_auth</option>
                <option value="openai_api">openai_api</option>
                <option value="slack_bot">slack_bot</option>
              </select>
            </div>
            <div className="field-group">
              <label className="field-label">Secret Token (Encrypted Immediately)</label>
              <input
                className="input"
                type="password"
                value={credSecret}
                onChange={(e) => setCredSecret(e.target.value)}
                placeholder="Will never be returned in plaintext"
              />
            </div>
            <button
              className="btn btn-primary"
              style={{ width: "100%", justifyContent: "center" }}
              disabled={!connected || !credName.trim() || !credSecret.trim()}
              onClick={async () => {
                await onCreateCredential(credName.trim(), credType, credSecret.trim());
                setCredName("");
                setCredSecret("");
              }}
            >
              <KeyRound size={13} /> Store Encrypted Credential
            </button>

            <div className="section-title" style={{ marginTop: 16 }}>
              Stored Credentials ({credentials.length})
            </div>
            {credentials.map((c) => (
              <div
                key={c.id}
                style={{
                  padding: 8,
                  borderRadius: 6,
                  border: "1px solid var(--border)",
                  background: "var(--bg-elevated)",
                  marginBottom: 6,
                }}
              >
                <div style={{ fontWeight: 600 }}>{c.name}</div>
                <div style={{ fontSize: 10, color: "var(--text-muted)", fontFamily: "monospace" }}>
                  {c.credential_type} · key:{c.key_id}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </aside>
  );
}

function extractLeafPaths(value: unknown, prefix: string, depth = 0): string[] {
  if (depth > 3 || value === null || value === undefined || typeof value !== "object") {
    return [prefix];
  }
  if (Array.isArray(value)) {
    return value.length > 0 ? [prefix, `${prefix}[0]`] : [prefix];
  }
  const entries = Object.entries(value as Record<string, unknown>).slice(0, 12);
  if (entries.length === 0) return [prefix];
  const paths: string[] = [];
  for (const [k, v] of entries) {
    if (!/^[a-zA-Z_][a-zA-Z0-9_]*$/.test(k)) continue;
    const next = `${prefix}.${k}`;
    if (v !== null && typeof v === "object" && !Array.isArray(v) && depth < 2) {
      paths.push(...extractLeafPaths(v, next, depth + 1));
    } else {
      paths.push(next);
    }
  }
  return paths.length > 0 ? paths : [prefix];
}

function NodeConfigEditor({
  node,
  catalogEntry,
  upstreamNodeIds,
  pinnedOutputs,
  triggerInputJson,
  credentials,
  onUpdate,
  onDelete,
}: {
  node: NodeDefinition;
  catalogEntry?: NodeCatalogEntry;
  upstreamNodeIds: string[];
  pinnedOutputs: Record<string, unknown>;
  triggerInputJson: string;
  credentials: CredentialMeta[];
  onUpdate: (updater: (n: NodeDefinition) => NodeDefinition) => void;
  onDelete: () => void;
}) {
  const properties = catalogEntry?.config_schema.properties ?? {};
  const requiredFields = new Set(catalogEntry?.config_schema.required ?? []);
  const credReqs = catalogEntry?.metadata.credentials ?? [];
  const [focusedField, setFocusedField] = useState<string | null>(null);
  const [previewExpr, setPreviewExpr] = useState("");
  const [previewResult, setPreviewResult] = useState<{
    ok: boolean;
    value: unknown;
    error: string | null;
  } | null>(null);

  const autocompleteTokens = useMemo(() => {
    const tokens: string[] = [];
    try {
      const parsedTrigger = JSON.parse(triggerInputJson || "{}");
      for (const path of extractLeafPaths(parsedTrigger, "trigger")) {
        tokens.push(`{{ ${path} }}`);
      }
    } catch {
      tokens.push("{{ trigger }}");
    }
    for (const uid of upstreamNodeIds) {
      const pinned = pinnedOutputs[uid];
      if (pinned !== undefined) {
        for (const path of extractLeafPaths(pinned, `nodes.${uid}.output`)) {
          tokens.push(`{{ ${path} }}`);
        }
      } else {
        tokens.push(`{{ nodes.${uid}.output }}`);
      }
    }
    return tokens.slice(0, 24);
  }, [upstreamNodeIds, pinnedOutputs, triggerInputJson]);

  const runLivePreview = async (expr: string) => {
    setPreviewExpr(expr);
    if (!expr.trim()) {
      setPreviewResult(null);
      return;
    }
    let triggerObj: Record<string, unknown> = {};
    try {
      triggerObj = JSON.parse(triggerInputJson || "{}");
    } catch {
      triggerObj = {};
    }
    try {
      const res = await previewExpressionApi(expr, pinnedOutputs, triggerObj);
      setPreviewResult(res);
    } catch (err) {
      setPreviewResult({
        ok: false,
        value: null,
        error: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const insertExpressionToken = (token: string) => {
    if (focusedField) {
      const cur = node.config[focusedField];
      const next = `${cur === undefined || cur === null ? "" : String(cur)}${token}`;
      updateConfigField(focusedField, next);
      void runLivePreview(next);
    } else {
      void runLivePreview(token);
      if (typeof navigator !== "undefined" && navigator.clipboard) {
        void navigator.clipboard.writeText(token).catch(() => undefined);
      }
    }
  };

  const updateConfigField = (key: string, val: unknown) => {
    onUpdate((n) => ({
      ...n,
      config: { ...n.config, [key]: val },
    }));
  };

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div>
          <div style={{ fontWeight: 700, fontSize: 13 }}>{node.name || node.id}</div>
          <div style={{ fontSize: 10, fontFamily: "monospace", color: "var(--text-muted)" }}>
            {node.type}
          </div>
        </div>
        <button className="btn btn-danger" style={{ height: 26 }} onClick={onDelete}>
          <Trash2 size={12} />
        </button>
      </div>

      <div className="section-title">Node Identity</div>
      <div className="field-group">
        <label className="field-label">Display Name</label>
        <input
          className="input"
          value={node.name}
          onChange={(e) => onUpdate((n) => ({ ...n, name: e.target.value }))}
        />
      </div>

      {autocompleteTokens.length > 0 && (
        <div className="field-group">
          <div className="field-label" style={{ display: "flex", alignItems: "center", gap: 4 }}>
            <Sparkles size={11} /> Clickable Expression Autocomplete{" "}
            {focusedField ? `(inserts into "${focusedField}")` : "(click to preview/copy)"}
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
            {autocompleteTokens.map((tok) => (
              <button
                key={tok}
                type="button"
                className="pill pill-info"
                style={{
                  fontFamily: "monospace",
                  textTransform: "none",
                  cursor: "pointer",
                  border: "none",
                }}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => insertExpressionToken(tok)}
                title="Click to insert into active field and preview live value"
              >
                {tok}
              </button>
            ))}
          </div>
          <div style={{ marginTop: 6 }}>
            <input
              className="input"
              style={{ fontSize: 11, fontFamily: "monospace" }}
              value={previewExpr}
              placeholder="Live expression preview e.g. {{ trigger.amount * 2 }}"
              onChange={(e) => void runLivePreview(e.target.value)}
            />
            {previewResult && (
              <div
                style={{
                  marginTop: 4,
                  padding: "4px 8px",
                  borderRadius: 6,
                  fontSize: 11,
                  fontFamily: "monospace",
                  background: previewResult.ok
                    ? "rgba(16, 185, 129, 0.12)"
                    : "rgba(239, 68, 68, 0.12)",
                  color: previewResult.ok ? "#34d399" : "#f87171",
                  wordBreak: "break-all",
                }}
              >
                {previewResult.ok
                  ? `=> ${JSON.stringify(previewResult.value)}`
                  : `Error: ${previewResult.error}`}
              </div>
            )}
          </div>
        </div>
      )}

      <div className="section-title">Schema Configuration</div>
      {Object.entries(properties).map(([key, prop]) => {
        const currentVal = node.config[key];
        const isObjOrArr = prop.type === "object" || prop.type === "array";

        if (prop.enum && prop.enum.length > 0) {
          return (
            <div className="field-group" key={key}>
              <label className="field-label">
                <span>
                  {prop.title ?? key} {requiredFields.has(key) ? "*" : ""}
                </span>
              </label>
              <select
                className="select"
                value={String(currentVal ?? prop.default ?? prop.enum[0])}
                onChange={(e) => updateConfigField(key, e.target.value)}
              >
                {prop.enum.map((opt) => (
                  <option key={opt} value={opt}>
                    {opt}
                  </option>
                ))}
              </select>
            </div>
          );
        }

        if (prop.type === "boolean") {
          return (
            <div className="field-group" key={key}>
              <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
                <input
                  type="checkbox"
                  checked={Boolean(currentVal ?? prop.default ?? false)}
                  onChange={(e) => updateConfigField(key, e.target.checked)}
                />
                <span>{prop.title ?? key}</span>
              </label>
            </div>
          );
        }

        if (isObjOrArr) {
          const textVal =
            typeof currentVal === "string"
              ? currentVal
              : JSON.stringify(currentVal ?? prop.default ?? {}, null, 2);
          return (
            <div className="field-group" key={key}>
              <label className="field-label">
                <span>{prop.title ?? key} (JSON / Expressions)</span>
              </label>
              <textarea
                className="textarea"
                rows={4}
                defaultValue={textVal}
                key={`${node.id}-${key}`}
                onBlur={(e) => {
                  try {
                    updateConfigField(key, JSON.parse(e.target.value));
                  } catch {
                    // keep valid JSON only
                  }
                }}
              />
            </div>
          );
        }

        return (
          <div className="field-group" key={key}>
            <label className="field-label">
              <span>
                {prop.title ?? key} {requiredFields.has(key) ? "*" : ""}
              </span>
            </label>
            <input
              className="input"
              value={currentVal === undefined || currentVal === null ? "" : String(currentVal)}
              placeholder={prop.default !== undefined ? String(prop.default) : "{{ nodes... }}"}
              onFocus={() => setFocusedField(key)}
              onChange={(e) => {
                const raw = e.target.value;
                if (raw.includes("{{")) {
                  void runLivePreview(raw);
                }
                if (
                  (prop.type === "integer" || prop.type === "number") &&
                  raw !== "" &&
                  !raw.includes("{{") &&
                  !Number.isNaN(Number(raw))
                ) {
                  updateConfigField(key, Number(raw));
                } else {
                  updateConfigField(key, raw);
                }
              }}
            />
          </div>
        );
      })}

      {credReqs.length > 0 && (
        <>
          <div className="section-title">Credentials (Referenced by ID Only)</div>
          {credReqs.map((req) => (
            <div className="field-group" key={req.slot}>
              <label className="field-label">
                <span>
                  Slot: {req.slot} ({req.credential_type})
                </span>
              </label>
              <select
                className="select"
                value={node.credentials[req.slot] ?? ""}
                onChange={(e) => {
                  const cid = e.target.value;
                  onUpdate((n) => {
                    const next = { ...n.credentials };
                    if (cid) next[req.slot] = cid;
                    else delete next[req.slot];
                    return { ...n, credentials: next };
                  });
                }}
              >
                <option value="">-- None --</option>
                {credentials.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name} ({c.credential_type})
                  </option>
                ))}
              </select>
            </div>
          ))}
        </>
      )}

      {catalogEntry?.metadata.is_loop && (
        <>
          <div className="section-title">Bounded Loop Sub-Graph</div>
          <div className="field-group">
            <label className="field-label">Items Expression *</label>
            <input
              className="input"
              value={node.loop?.items_expression ?? "{{ trigger.items }}"}
              onChange={(e) =>
                onUpdate((n) => ({
                  ...n,
                  loop: {
                    items_expression: e.target.value,
                    item_variable: n.loop?.item_variable ?? "item",
                    index_variable: n.loop?.index_variable ?? "index",
                    max_iterations: n.loop?.max_iterations ?? 100,
                    fail_fast: n.loop?.fail_fast ?? true,
                    body_graph: n.loop?.body_graph ?? {
                      nodes: [
                        {
                          id: "loop_step_1",
                          type: "core.set_data",
                          name: "Process Item",
                          description: "",
                          config: { values: { item: "{{ loop.item }}", idx: "{{ loop.index }}" } },
                          credentials: {},
                          policy: n.policy,
                        },
                      ],
                      edges: [],
                    },
                  },
                }))
              }
            />
          </div>
          <div className="field-group">
            <label className="field-label">Max Iterations (1..10000)</label>
            <input
              className="input"
              type="number"
              value={node.loop?.max_iterations ?? 100}
              onChange={(e) =>
                onUpdate((n) => ({
                  ...n,
                  loop: n.loop
                    ? { ...n.loop, max_iterations: Math.max(1, Number(e.target.value) || 100) }
                    : undefined,
                }))
              }
            />
          </div>
          <div className="field-group">
            <label className="field-label">
              Loop Body Nodes ({node.loop?.body_graph.nodes.length ?? 0})
            </label>
            <div style={{ fontSize: 11, color: "var(--text-secondary)" }}>
              {(node.loop?.body_graph.nodes ?? []).map((bn) => (
                <div
                  key={bn.id}
                  style={{
                    padding: "5px 8px",
                    borderRadius: 6,
                    background: "var(--bg-app)",
                    border: "1px solid var(--border)",
                    marginBottom: 4,
                    fontFamily: "monospace",
                  }}
                >
                  {bn.id} · {bn.type}
                </div>
              ))}
            </div>
          </div>
        </>
      )}

      <div className="section-title">Execution Policy & Safety</div>
      <div className="field-group">
        <label className="field-label">Timeout (ms)</label>
        <input
          className="input"
          type="number"
          value={node.policy.timeout_ms}
          onChange={(e) =>
            onUpdate((n) => ({
              ...n,
              policy: { ...n.policy, timeout_ms: Math.max(100, Number(e.target.value) || 30000) },
            }))
          }
        />
      </div>
      <div className="field-group">
        <label className="field-label">Idempotency Key Expression</label>
        <input
          className="input"
          value={node.policy.idempotency_key_expression ?? ""}
          placeholder="e.g. order-{{ trigger.order_id }}"
          onChange={(e) =>
            onUpdate((n) => ({
              ...n,
              policy: {
                ...n.policy,
                idempotency_key_expression: e.target.value || null,
              },
            }))
          }
        />
      </div>
      <div className="field-group">
        <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={node.policy.require_approval}
            onChange={(e) =>
              onUpdate((n) => ({
                ...n,
                policy: { ...n.policy, require_approval: e.target.checked },
              }))
            }
          />
          <span>Require Human Approval Before Execution</span>
        </label>
      </div>
    </div>
  );
}
