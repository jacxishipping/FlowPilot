"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Connection,
  type NodeChange,
  type NodeTypes,
} from "@xyflow/react";
import {
  CheckSquare,
  Cloud,
  GitCommitHorizontal,
  LayoutGrid,
  LogIn,
  Play,
  Plus,
  Redo2,
  Rocket,
  Save,
  Sparkles,
  Undo2,
} from "lucide-react";
import {
  FALLBACK_NODE_CATALOG,
  bootstrapDevTenant,
  createCredentialApi,
  createWorkflow,
  fetchNodeCatalog,
  fetchRunEventsSse,
  fetchWorkflowJsonSchema,
  generateWorkflowDraftApi,
  listCredentialsApi,
  listProjectsApi,
  listWorkflowVersionsApi,
  listWorkflowsApi,
  loginAccountApi,
  patchWorkflow,
  publishWorkflowApi,
  registerAccountApi,
  startWorkflowRunApi,
  submitApprovalDecisionApi,
  validateWorkflowApi,
} from "@/lib/api";
import {
  autoLayoutGraph,
  canonicalToCanvas,
  createStarterWorkflowGraph,
  defaultPolicy,
} from "@/lib/graphAdapter";
import type {
  BootstrapSession,
  CredentialMeta,
  EdgeKind,
  ExecutionEvent,
  NodeCatalogEntry,
  NodeDefinition,
  ValidationResult,
  WorkflowGraph,
  WorkflowRecord,
  WorkflowRunState,
} from "@/lib/types";
import { AiDraftModal } from "./AiDraftModal";
import { AuthModal } from "./AuthModal";
import { FlowNode } from "./FlowNode";
import { InspectorPanel, type InspectorTab } from "./InspectorPanel";
import { NodeLibrary } from "./NodeLibrary";
import { VersionHistoryModal, type VersionSummary } from "./VersionHistoryModal";

const NODE_TYPES: NodeTypes = { flowNode: FlowNode };

function WorkflowEditorInner() {
  const { screenToFlowPosition } = useReactFlow();
  const wrapperRef = useRef<HTMLDivElement | null>(null);

  // 1. Graph State (canonical, canvas-independent + undo/redo stack)
  const [workflowName, setWorkflowName] = useState("Order Approval Pipeline");
  const [graph, setGraph] = useState<WorkflowGraph>(() => createStarterWorkflowGraph());
  const [undoStack, setUndoStack] = useState<WorkflowGraph[]>([]);
  const [redoStack, setRedoStack] = useState<WorkflowGraph[]>([]);

  // 2. Editor State
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>("trigger");
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<InspectorTab>("configure");
  const [aiModalOpen, setAiModalOpen] = useState(false);
  const [aiLoading, setAiLoading] = useState(false);
  const [authModalOpen, setAuthModalOpen] = useState(false);
  const [authLoading, setAuthLoading] = useState(false);
  const [versionModalOpen, setVersionModalOpen] = useState(false);
  const [versions, setVersions] = useState<VersionSummary[]>([]);
  const [savedWorkflows, setSavedWorkflows] = useState<WorkflowRecord[]>([]);
  const [statusBanner, setStatusBanner] = useState<string>("Ready — offline preview or connect API");

  // 3. Server State
  const [catalog, setCatalog] = useState<NodeCatalogEntry[]>(FALLBACK_NODE_CATALOG);
  const [workflowJsonSchema, setWorkflowJsonSchema] = useState<Record<string, unknown> | null>(null);
  const [session, setSession] = useState<BootstrapSession | null>(null);
  const [workflowRecord, setWorkflowRecord] = useState<WorkflowRecord | null>(null);
  const [publishedVersion, setPublishedVersion] = useState<number | null>(null);
  const [validation, setValidation] = useState<ValidationResult | null>(null);
  const [credentials, setCredentials] = useState<CredentialMeta[]>([]);

  // 4. Execution State
  const [triggerInputJson, setTriggerInputJson] = useState(
    JSON.stringify({ amount: 350, customer_email: "CFO@Acme.io" }, null, 2),
  );
  const [activeRun, setActiveRun] = useState<WorkflowRunState | null>(null);
  const [runEvents, setRunEvents] = useState<ExecutionEvent[]>([]);

  // Load live NodeRegistry catalog & WorkflowDefinition JSON Schema on mount
  useEffect(() => {
    let active = true;
    Promise.all([fetchNodeCatalog(), fetchWorkflowJsonSchema()])
      .then(([nodes, schema]) => {
        if (!active) return;
        setCatalog(nodes);
        setWorkflowJsonSchema(schema);
        setStatusBanner(`Loaded ${nodes.length} nodes & canonical schema from API`);
      })
      .catch(() => {
        if (!active) return;
        setStatusBanner("API offline — using local catalog (start FastAPI on :8000 to run)");
      });
    return () => {
      active = false;
    };
  }, []);

  const catalogByType = useMemo(() => {
    const map = new Map<string, NodeCatalogEntry>();
    for (const item of catalog) map.set(item.metadata.type, item);
    return map;
  }, [catalog]);

  const commitGraph = useCallback(
    (updater: (prev: WorkflowGraph) => WorkflowGraph) => {
      setGraph((prev) => {
        const next = updater(prev);
        setUndoStack((s) => [...s.slice(-24), prev]);
        setRedoStack([]);
        return next;
      });
    },
    [],
  );

  const handleUndo = useCallback(() => {
    setUndoStack((stack) => {
      if (stack.length === 0) return stack;
      const previous = stack[stack.length - 1];
      setRedoStack((r) => [...r, graph]);
      setGraph(previous);
      return stack.slice(0, -1);
    });
  }, [graph]);

  const handleRedo = useCallback(() => {
    setRedoStack((stack) => {
      if (stack.length === 0) return stack;
      const next = stack[stack.length - 1];
      setUndoStack((u) => [...u, graph]);
      setGraph(next);
      return stack.slice(0, -1);
    });
  }, [graph]);

  const handleAddNode = useCallback(
    (entry: NodeCatalogEntry, position?: { x: number; y: number }) => {
      const baseSlug = entry.metadata.type.split(".").pop() ?? "node";
      commitGraph((prev) => {
        let suffix = prev.nodes.length + 1;
        let id = `${baseSlug}_${suffix}`;
        const existing = new Set(prev.nodes.map((n) => n.id));
        while (existing.has(id)) {
          suffix += 1;
          id = `${baseSlug}_${suffix}`;
        }
        const defaultConfig: Record<string, unknown> = {};
        for (const [key, prop] of Object.entries(entry.config_schema.properties ?? {})) {
          if (prop.default !== undefined) defaultConfig[key] = prop.default;
        }
        const newNode: NodeDefinition = {
          id,
          type: entry.metadata.type,
          name: entry.metadata.name,
          description: entry.metadata.description,
          config: defaultConfig,
          credentials: {},
          policy: defaultPolicy(),
          ui: {
            position: position ?? { x: 140 + prev.nodes.length * 40, y: 140 + prev.nodes.length * 30 },
          },
        };
        setSelectedNodeId(id);
        setSelectedEdgeId(null);
        return { ...prev, nodes: [...prev.nodes, newNode] };
      });
    },
    [commitGraph],
  );

  const handleConnect = useCallback(
    (connection: Connection) => {
      if (!connection.source || !connection.target) return;
      const handle = connection.sourceHandle ?? "default";
      const kind: EdgeKind =
        handle === "true" || handle === "false" || handle === "error" ? handle : "default";
      const edgeId = `e_${connection.source}_${connection.target}_${kind}_${Date.now() % 10000}`;
      commitGraph((prev) => ({
        ...prev,
        edges: [
          ...prev.edges,
          {
            id: edgeId,
            source: connection.source!,
            target: connection.target!,
            kind,
            source_handle: handle,
            target_handle: connection.targetHandle ?? "in",
          },
        ],
      }));
    },
    [commitGraph],
  );

  const handleNodesChange = useCallback((changes: NodeChange[]) => {
    const posUpdates = new Map<string, { x: number; y: number }>();
    for (const ch of changes) {
      if (ch.type === "position" && ch.position) {
        posUpdates.set(ch.id, ch.position);
      }
    }
    if (posUpdates.size === 0) return;
    setGraph((prev) => ({
      ...prev,
      nodes: prev.nodes.map((n) => {
        const pos = posUpdates.get(n.id);
        if (!pos) return n;
        return { ...n, ui: { ...(n.ui ?? { position: pos }), position: pos } };
      }),
    }));
  }, []);

  const ensureConnectedWorkflow = useCallback(async (): Promise<{
    sess: BootstrapSession;
    wf: WorkflowRecord;
  }> => {
    let sess = session;
    if (!sess) {
      const slug = `studio-${Date.now().toString(36)}`;
      sess = await bootstrapDevTenant(slug);
      setSession(sess);
      const creds = await listCredentialsApi(sess.api_key);
      setCredentials(creds);
    }

    let wf = workflowRecord;
    if (!wf) {
      wf = await createWorkflow(sess.api_key, {
        project_id: sess.project_id,
        name: workflowName,
        description: "Created in FlowPilot Visual Studio",
        graph,
      });
      setWorkflowRecord(wf);
    } else {
      wf = await patchWorkflow(sess.api_key, wf.id, {
        expected_revision: wf.revision,
        name: workflowName,
        graph,
      });
      setWorkflowRecord(wf);
    }
    return { sess, wf };
  }, [session, workflowRecord, workflowName, graph]);

  const handleSave = async () => {
    try {
      const { sess, wf } = await ensureConnectedWorkflow();
      const allWfs = await listWorkflowsApi(sess.api_key);
      setSavedWorkflows(allWfs);
      setStatusBanner(`Saved draft '${wf.name}' at revision r${wf.revision}`);
    } catch (err) {
      setStatusBanner(`Save failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const handleOpenVersionHistory = async () => {
    try {
      if (session && workflowRecord) {
        const list = await listWorkflowVersionsApi(session.api_key, workflowRecord.id);
        setVersions(list);
      }
      setVersionModalOpen(true);
    } catch (err) {
      setStatusBanner(`Failed to load versions: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const handleRegister = async (
    email: string,
    password: string,
    orgName: string,
    orgSlug: string,
  ) => {
    setAuthLoading(true);
    try {
      const reg = await registerAccountApi({
        email,
        password,
        organization_name: orgName,
        organization_slug: orgSlug,
      });
      setSession({
        organization_id: reg.organization_id,
        workspace_id: reg.workspace_id,
        project_id: reg.project_id,
        api_key: reg.access_token,
      });
      setAuthModalOpen(false);
      setStatusBanner(`Signed in as ${reg.email} (${reg.role})`);
    } catch (err) {
      setStatusBanner(`Registration failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setAuthLoading(false);
    }
  };

  const handleLogin = async (email: string, password: string) => {
    setAuthLoading(true);
    try {
      const res = await loginAccountApi({ email, password });
      const projects = await listProjectsApi(res.access_token);
      const firstProj = projects[0];
      setSession({
        organization_id: res.organization_id,
        workspace_id: firstProj?.workspace_id ?? "",
        project_id: firstProj?.id ?? "",
        api_key: res.access_token,
      });
      const wfs = await listWorkflowsApi(res.access_token);
      setSavedWorkflows(wfs);
      setAuthModalOpen(false);
      setStatusBanner(`Signed in as ${res.email} (${res.role})`);
    } catch (err) {
      setStatusBanner(`Login failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setAuthLoading(false);
    }
  };

  const handleNewWorkflow = () => {
    setWorkflowRecord(null);
    setPublishedVersion(null);
    setWorkflowName(`Workflow ${savedWorkflows.length + 1}`);
    commitGraph(() => createStarterWorkflowGraph());
    setStatusBanner("Started new local workflow draft");
  };

  const handleValidate = async () => {
    try {
      const { sess, wf } = await ensureConnectedWorkflow();
      const result = await validateWorkflowApi(sess.api_key, wf.id);
      setValidation(result);
      setActiveTab("validate");
      const errs = result.issues.filter((i) => i.severity === "error").length;
      setStatusBanner(
        errs === 0 ? "Validation passed (0 errors)" : `Validation found ${errs} error(s)`,
      );
    } catch (err) {
      setStatusBanner(`Validate failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const handlePublish = async () => {
    try {
      const { sess, wf } = await ensureConnectedWorkflow();
      const ver = await publishWorkflowApi(sess.api_key, wf.id);
      setPublishedVersion(ver.version_number);
      setValidation({ issues: [] });
      setStatusBanner(`Published immutable v${ver.version_number} (${ver.content_hash.slice(0, 8)})`);
    } catch (err) {
      setStatusBanner(`Publish failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const handleRun = async () => {
    try {
      const { sess, wf } = await ensureConnectedWorkflow();
      if (publishedVersion === null) {
        const ver = await publishWorkflowApi(sess.api_key, wf.id, "Auto-publish before test run");
        setPublishedVersion(ver.version_number);
      }
      const parsedInput = JSON.parse(triggerInputJson) as Record<string, unknown>;
      const run = await startWorkflowRunApi(sess.api_key, wf.id, parsedInput);
      setActiveRun(run);
      const events = await fetchRunEventsSse(sess.api_key, run.id);
      setRunEvents(events);
      setActiveTab("runs");
      setStatusBanner(`Run ${run.id.slice(0, 8)} -> ${run.status}`);
    } catch (err) {
      setStatusBanner(`Run failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const handleDecideApproval = async (approvalId: string, approved: boolean, comment: string) => {
    if (!session) return;
    try {
      const resumed = await submitApprovalDecisionApi(session.api_key, approvalId, approved, comment);
      setActiveRun(resumed);
      const events = await fetchRunEventsSse(session.api_key, resumed.id);
      setRunEvents(events);
      setStatusBanner(`Approval ${approved ? "approved" : "rejected"} -> Run ${resumed.status}`);
    } catch (err) {
      setStatusBanner(`Decision failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const handleCreateCredential = async (name: string, type: string, token: string) => {
    const { sess } = await ensureConnectedWorkflow();
    const created = await createCredentialApi(sess.api_key, {
      name,
      credential_type: type,
      secrets: { token },
    });
    setCredentials((prev) => [...prev, created]);
    setStatusBanner(`Encrypted credential '${created.name}' stored (key: ${created.key_id})`);
  };

  const handleGenerateAiDraft = async (prompt: string) => {
    setAiLoading(true);
    try {
      const { sess } = await ensureConnectedWorkflow();
      const resp = await generateWorkflowDraftApi(sess.api_key, sess.project_id, prompt);
      setWorkflowName(resp.draft.name);
      commitGraph(() => resp.draft.graph);
      setValidation(resp.validation);
      setAiModalOpen(false);
      setActiveTab("validate");
      setStatusBanner(
        `AI generated editable draft (${resp.draft.graph.nodes.length} nodes) — review before publishing`,
      );
    } catch (err) {
      setStatusBanner(`AI generation failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setAiLoading(false);
    }
  };

  const nodeStatuses = useMemo(() => {
    const map: Record<string, { status: WorkflowRunState["node_executions"][string]["status"]; duration_ms?: number | null }> = {};
    if (!activeRun) return map;
    for (const [id, exec] of Object.entries(activeRun.node_executions)) {
      map[id] = { status: exec.status, duration_ms: exec.duration_ms };
    }
    return map;
  }, [activeRun]);

  const errorNodeIds = useMemo(() => {
    const set = new Set<string>();
    for (const issue of validation?.issues ?? []) {
      if (issue.severity === "error" && issue.node_id) set.add(issue.node_id);
    }
    return set;
  }, [validation]);

  const { nodes: canvasNodes, edges: canvasEdges } = useMemo(
    () => canonicalToCanvas(graph, catalogByType, nodeStatuses, errorNodeIds),
    [graph, catalogByType, nodeStatuses, errorNodeIds],
  );

  const selectedNode = useMemo(
    () => graph.nodes.find((n) => n.id === selectedNodeId) ?? null,
    [graph.nodes, selectedNodeId],
  );

  const selectedEdge = useMemo(
    () => graph.edges.find((e) => e.id === selectedEdgeId) ?? null,
    [graph.edges, selectedEdgeId],
  );

  const upstreamNodeIds = useMemo(() => {
    if (!selectedNodeId) return [];
    const parents = new Set<string>();
    const queue = [selectedNodeId];
    while (queue.length > 0) {
      const cur = queue.shift()!;
      for (const e of graph.edges) {
        if (e.target === cur && !parents.has(e.source)) {
          parents.add(e.source);
          queue.push(e.source);
        }
      }
    }
    return Array.from(parents);
  }, [graph.edges, selectedNodeId]);

  return (
    <div className="studio-shell">
      <header className="topbar">
        <div className="topbar-group">
          <div className="brand">
            <span className="brand-mark">FP</span>
            <span>FlowPilot Studio</span>
          </div>
          {savedWorkflows.length > 0 && (
            <select
              className="select"
              style={{ width: 170, height: 30 }}
              value={workflowRecord?.id ?? ""}
              onChange={(e) => {
                const found = savedWorkflows.find((w) => w.id === e.target.value);
                if (found) {
                  setWorkflowRecord(found);
                  setWorkflowName(found.name);
                  setGraph(found.definition.graph);
                  setStatusBanner(`Loaded workflow '${found.name}' (r${found.revision})`);
                }
              }}
            >
              <option value="">-- Switch Workflow --</option>
              {savedWorkflows.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name} (r{w.revision})
                </option>
              ))}
            </select>
          )}
          <input
            className="input"
            style={{ width: 200, height: 30 }}
            value={workflowName}
            onChange={(e) => setWorkflowName(e.target.value)}
          />
          <button className="btn" style={{ height: 30 }} onClick={handleNewWorkflow} title="New Workflow">
            <Plus size={13} />
          </button>
          <span className="pill">
            {workflowRecord ? `r${workflowRecord.revision}` : "local draft"}
          </span>
          {publishedVersion !== null && (
            <span className="pill pill-success">v{publishedVersion} published</span>
          )}
        </div>

        <div className="topbar-group">
          <button className="btn" onClick={() => setAuthModalOpen(true)} title="Sign In / Register">
            <LogIn size={13} /> {session ? "Account" : "Sign In"}
          </button>
          <button className="btn" onClick={handleOpenVersionHistory} title="Version History">
            <GitCommitHorizontal size={13} /> Versions
          </button>
          <button className="btn" onClick={handleUndo} disabled={undoStack.length === 0} title="Undo">
            <Undo2 size={13} />
          </button>
          <button className="btn" onClick={handleRedo} disabled={redoStack.length === 0} title="Redo">
            <Redo2 size={13} />
          </button>
          <button
            className="btn"
            onClick={() => commitGraph((g) => autoLayoutGraph(g))}
            title="Auto-layout DAG"
          >
            <LayoutGrid size={13} /> Layout
          </button>
          <button className="btn" onClick={() => setAiModalOpen(true)}>
            <Sparkles size={13} color="#c084fc" /> AI Draft
          </button>
          <button className="btn" onClick={handleValidate}>
            <CheckSquare size={13} /> Validate
          </button>
          <button className="btn" onClick={handleSave}>
            <Save size={13} /> Save
          </button>
          <button className="btn btn-primary" onClick={handlePublish}>
            <Rocket size={13} /> Publish
          </button>
          <button className="btn btn-success" onClick={handleRun}>
            <Play size={13} /> Run
          </button>
        </div>
      </header>

      <div className="workspace-body">
        <NodeLibrary catalog={catalog} onAddNode={(entry) => handleAddNode(entry)} />

        <main
          className="canvas-center"
          ref={wrapperRef}
          onDragOver={(ev) => {
            ev.preventDefault();
            ev.dataTransfer.dropEffect = "move";
          }}
          onDrop={(ev) => {
            ev.preventDefault();
            const nodeType = ev.dataTransfer.getData("application/flowpilot-node");
            const entry = catalogByType.get(nodeType);
            if (!entry) return;
            const pos = screenToFlowPosition({ x: ev.clientX, y: ev.clientY });
            handleAddNode(entry, pos);
          }}
        >
          <ReactFlow
            nodes={canvasNodes}
            edges={canvasEdges}
            nodeTypes={NODE_TYPES}
            onNodesChange={handleNodesChange}
            onConnect={handleConnect}
            onNodeClick={(_, node) => {
              setSelectedNodeId(node.id);
              setSelectedEdgeId(null);
              setActiveTab("configure");
            }}
            onEdgeClick={(_, edge) => {
              setSelectedEdgeId(edge.id);
              setSelectedNodeId(null);
              setActiveTab("configure");
            }}
            onPaneClick={() => {
              setSelectedNodeId(null);
              setSelectedEdgeId(null);
            }}
            fitView
            colorMode="dark"
          >
            <Background color="#1e273b" gap={20} />
            <Controls />
            <MiniMap nodeColor="#6366f1" maskColor="rgba(11, 14, 20, 0.7)" />
          </ReactFlow>

          <div
            style={{
              position: "absolute",
              left: 14,
              bottom: 14,
              padding: "6px 10px",
              borderRadius: 8,
              background: "rgba(17, 22, 34, 0.92)",
              border: "1px solid var(--border)",
              fontSize: 11,
              color: "var(--text-secondary)",
              display: "flex",
              alignItems: "center",
              gap: 6,
            }}
          >
            <Cloud size={12} color={session ? "#34d399" : "#9aa7c2"} />
            <span>{statusBanner}</span>
          </div>
        </main>

        <InspectorPanel
          activeTab={activeTab}
          onTabChange={setActiveTab}
          selectedNode={selectedNode}
          selectedEdge={selectedEdge}
          upstreamNodeIds={upstreamNodeIds}
          catalogEntry={selectedNode ? catalogByType.get(selectedNode.type) : undefined}
          validation={validation}
          workflowJsonSchema={workflowJsonSchema}
          activeRun={activeRun}
          runEvents={runEvents}
          credentials={credentials}
          triggerInputJson={triggerInputJson}
          onTriggerInputChange={setTriggerInputJson}
          onUpdateNode={(nodeId, updater) =>
            commitGraph((prev) => ({
              ...prev,
              nodes: prev.nodes.map((n) => (n.id === nodeId ? updater(n) : n)),
            }))
          }
          onDeleteNode={(nodeId) => {
            commitGraph((prev) => ({
              nodes: prev.nodes.filter((n) => n.id !== nodeId),
              edges: prev.edges.filter((e) => e.source !== nodeId && e.target !== nodeId),
            }));
            setSelectedNodeId(null);
          }}
          onUpdateEdge={(edgeId, kind) =>
            commitGraph((prev) => ({
              ...prev,
              edges: prev.edges.map((e) =>
                e.id === edgeId
                  ? {
                      ...e,
                      kind,
                      source_handle: kind === "default" ? "default" : kind,
                      label: kind === "default" ? null : kind.toUpperCase(),
                    }
                  : e,
              ),
            }))
          }
          onDeleteEdge={(edgeId) => {
            commitGraph((prev) => ({
              ...prev,
              edges: prev.edges.filter((e) => e.id !== edgeId),
            }));
            setSelectedEdgeId(null);
          }}
          onSelectNodeId={(nid) => {
            setSelectedNodeId(nid);
            setSelectedEdgeId(null);
            setActiveTab("configure");
          }}
          onRunWorkflow={handleRun}
          onDecideApproval={handleDecideApproval}
          onCreateCredential={handleCreateCredential}
          connected={Boolean(session) || true}
        />
      </div>

      <AiDraftModal
        open={aiModalOpen}
        loading={aiLoading}
        onClose={() => setAiModalOpen(false)}
        onGenerate={handleGenerateAiDraft}
      />

      <AuthModal
        open={authModalOpen}
        loading={authLoading}
        onClose={() => setAuthModalOpen(false)}
        onLogin={handleLogin}
        onRegister={handleRegister}
      />

      <VersionHistoryModal
        open={versionModalOpen}
        versions={versions}
        currentRevision={workflowRecord?.revision ?? 1}
        onClose={() => setVersionModalOpen(false)}
      />
    </div>
  );
}

export function WorkflowEditor() {
  return (
    <ReactFlowProvider>
      <WorkflowEditorInner />
    </ReactFlowProvider>
  );
}
