"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Background,
  BackgroundVariant,
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
  BarChart3,
  Bot,
  CheckSquare,
  Database,
  Download,
  GitCommitHorizontal,
  Inbox,
  LayoutGrid,
  LayoutTemplate,
  LogIn,
  MessageSquare,
  Moon,
  MoreHorizontal,
  PanelLeft,
  PanelRight,
  Play,
  Plus,
  Redo2,
  Rocket,
  Save,
  Sparkles,
  Sun,
  Undo2,
  Upload,
} from "lucide-react";
import {
  FALLBACK_NODE_CATALOG,
  type InboxApprovalItem,
  type TemplateSummary,
  type WorkflowDiffResult,
  acquireWorkflowLockApi,
  bootstrapDevTenant,
  buildChatEmbedSnippet,
  createCredentialApi,
  createWorkflow,
  diffWorkflowVersionsApi,
  fetchNodeCatalog,
  fetchRunEventsSse,
  fetchWorkflowJsonSchema,
  generateApprovalActionLinksApi,
  generateWorkflowDraftApi,
  listApprovalsInboxApi,
  listCredentialsApi,
  listProjectsApi,
  listTemplatesApi,
  listWorkflowVersionsApi,
  listWorkflowsApi,
  loginAccountApi,
  patchWorkflow,
  publishWorkflowApi,
  registerAccountApi,
  replayRunFromNodeApi,
  rollbackWorkflowApi,
  runAiAssistantApi,
  startWorkflowRunApi,
  submitApprovalDecisionApi,
  testSingleNodeApi,
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
import { AiAssistantModal } from "./AiAssistantModal";
import { AiDraftModal } from "./AiDraftModal";
import { ApprovalInboxModal } from "./ApprovalInboxModal";
import { AuthModal } from "./AuthModal";
import { FlowNode } from "./FlowNode";
import { InspectorPanel, type InspectorTab } from "./InspectorPanel";
import { NodeLibrary } from "./NodeLibrary";
import { TemplatesModal } from "./TemplatesModal";
import { VersionHistoryModal, type VersionSummary } from "./VersionHistoryModal";
import { type HubTab, WorkspaceHubModal } from "./WorkspaceHubModal";

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
  const [diffResult, setDiffResult] = useState<WorkflowDiffResult | null>(null);
  const [templatesModalOpen, setTemplatesModalOpen] = useState(false);
  const [templates, setTemplates] = useState<TemplateSummary[]>([]);
  const [inboxModalOpen, setInboxModalOpen] = useState(false);
  const [inboxItems, setInboxItems] = useState<InboxApprovalItem[]>([]);
  const [pinnedOutputs, setPinnedOutputs] = useState<Record<string, unknown>>({});
  const [savedWorkflows, setSavedWorkflows] = useState<WorkflowRecord[]>([]);
  const [statusBanner, setStatusBanner] = useState<string>("Ready");
  const [theme, setTheme] = useState<"light" | "dark">("light");
  const [leftPanelOpen, setLeftPanelOpen] = useState(true);
  const [rightPanelOpen, setRightPanelOpen] = useState(true);
  const [isCompactViewport, setIsCompactViewport] = useState(false);
  const [moreMenuOpen, setMoreMenuOpen] = useState(false);
  const [hubModalOpen, setHubModalOpen] = useState(false);
  const [hubTab, setHubTab] = useState<HubTab>("insights");
  const [assistantOpen, setAssistantOpen] = useState(false);
  const [assistantLoading, setAssistantLoading] = useState(false);
  const [autosaveEnabled, setAutosaveEnabled] = useState(false);
  const [lockOwner, setLockOwner] = useState<string | null>(null);
  const importFileRef = useRef<HTMLInputElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  // Sync theme preference to <html data-theme="...">
  useEffect(() => {
    const saved = typeof window !== "undefined" ? window.localStorage.getItem("fp-theme") : null;
    if (saved === "dark" || saved === "light") {
      setTheme(saved);
      document.documentElement.dataset.theme = saved;
    } else {
      document.documentElement.dataset.theme = "light";
    }
  }, []);

  const toggleTheme = () => {
    setTheme((prev) => {
      const next = prev === "light" ? "dark" : "light";
      document.documentElement.dataset.theme = next;
      window.localStorage.setItem("fp-theme", next);
      return next;
    });
  };

  // Auto-collapse sidebars into clean overlay drawers on <= 1024px viewports
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 1024px)");
    const apply = (matches: boolean) => {
      setIsCompactViewport(matches);
      if (matches) {
        setLeftPanelOpen(false);
        setRightPanelOpen(false);
      } else {
        setLeftPanelOpen(true);
        setRightPanelOpen(true);
      }
    };
    apply(mq.matches);
    const handler = (ev: MediaQueryListEvent) => apply(ev.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);

  // Close overflow menu on outside click or Escape
  useEffect(() => {
    if (!moreMenuOpen) return;
    const onDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMoreMenuOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMoreMenuOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [moreMenuOpen]);

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

  // Debounced autosave & collaborative edit lock renewal when enabled
  useEffect(() => {
    if (!autosaveEnabled || !session || !workflowRecord) return;
    const timer = window.setTimeout(async () => {
      try {
        const lock = await acquireWorkflowLockApi(session.api_key, workflowRecord.id);
        setLockOwner(lock.locked_by);
        const updated = await patchWorkflow(session.api_key, workflowRecord.id, {
          expected_revision: workflowRecord.revision,
          name: workflowName,
          graph,
        });
        setWorkflowRecord(updated);
        setStatusBanner(`Autosaved r${updated.revision}`);
      } catch (err) {
        setStatusBanner(
          `Autosave paused: ${err instanceof Error ? err.message : String(err)}`,
        );
      }
    }, 2500);
    return () => window.clearTimeout(timer);
  }, [autosaveEnabled, session, workflowRecord, workflowName, graph]);

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

  const ensureSession = useCallback(async (): Promise<BootstrapSession> => {
    if (session) return session;
    const slug = `studio-${Date.now().toString(36)}`;
    const sess = await bootstrapDevTenant(slug);
    setSession(sess);
    const creds = await listCredentialsApi(sess.api_key);
    setCredentials(creds);
    return sess;
  }, [session]);

  const ensureConnectedWorkflow = useCallback(async (): Promise<{
    sess: BootstrapSession;
    wf: WorkflowRecord;
  }> => {
    const sess = await ensureSession();

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
    setDiffResult(null);
    setWorkflowName(`Workflow ${savedWorkflows.length + 1}`);
    commitGraph(() => createStarterWorkflowGraph());
    setStatusBanner("Started new local workflow draft");
  };

  const handleOpenTemplates = async () => {
    try {
      const list = await listTemplatesApi();
      setTemplates(list);
    } catch {
      setTemplates([
        {
          slug: "invoice-approval",
          name: "Invoice Approval Pipeline",
          category: "finance",
          description: "Routes high-value invoices through a human approval gate.",
          tags: ["finance", "approval"],
          node_count: 5,
          graph: createStarterWorkflowGraph(),
        },
      ]);
    }
    setTemplatesModalOpen(true);
  };

  const handleOpenInbox = async () => {
    if (session) {
      try {
        const items = await listApprovalsInboxApi(session.api_key);
        setInboxItems(items);
      } catch {
        // ignore if offline
      }
    }
    setInboxModalOpen(true);
  };

  const handleCompareVersions = async (fromVersion: number, toVersion: number) => {
    if (!session || !workflowRecord) return;
    try {
      const res = await diffWorkflowVersionsApi(
        session.api_key,
        workflowRecord.id,
        fromVersion,
        toVersion,
      );
      setDiffResult(res);
      setStatusBanner(`Highlighted diff v${fromVersion} -> v${toVersion} on canvas`);
    } catch (err) {
      setStatusBanner(`Diff failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const handleRollbackVersion = async (versionNumber: number) => {
    if (!session || !workflowRecord) return;
    try {
      const res = await rollbackWorkflowApi(session.api_key, workflowRecord.id, versionNumber);
      commitGraph(() => res.definition.graph);
      setPublishedVersion(res.new_version_number);
      setVersionModalOpen(false);
      setStatusBanner(`Rolled back to v${versionNumber} (published as v${res.new_version_number})`);
    } catch (err) {
      setStatusBanner(`Rollback failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const handleReplayFromNode = async (startNodeId: string) => {
    if (!session || !activeRun) return;
    try {
      const forked = await replayRunFromNodeApi(
        session.api_key,
        activeRun.id,
        startNodeId,
        pinnedOutputs,
      );
      setActiveRun(forked);
      const events = await fetchRunEventsSse(session.api_key, forked.id);
      setRunEvents(events);
      setStatusBanner(`Time-travel replayed from '${startNodeId}' -> ${forked.status}`);
    } catch (err) {
      setStatusBanner(`Replay failed: ${err instanceof Error ? err.message : String(err)}`);
    }
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

  const diffHighlights = useMemo(() => {
    const map: Record<string, "added" | "modified" | "removed"> = {};
    if (!diffResult) return map;
    for (const id of diffResult.added_nodes) map[id] = "added";
    for (const m of diffResult.modified_nodes) map[m.node_id] = "modified";
    for (const id of diffResult.removed_nodes) map[id] = "removed";
    return map;
  }, [diffResult]);

  const pinnedNodeIds = useMemo(() => new Set(Object.keys(pinnedOutputs)), [pinnedOutputs]);

  const { nodes: canvasNodes, edges: canvasEdges } = useMemo(
    () =>
      canonicalToCanvas(
        graph,
        catalogByType,
        nodeStatuses,
        errorNodeIds,
        diffHighlights,
        pinnedNodeIds,
      ),
    [graph, catalogByType, nodeStatuses, errorNodeIds, diffHighlights, pinnedNodeIds],
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

  const openLeftDrawer = () => {
    setLeftPanelOpen((prev) => {
      const next = !prev;
      if (next && isCompactViewport) setRightPanelOpen(false);
      return next;
    });
  };

  const openRightDrawer = () => {
    setRightPanelOpen((prev) => {
      const next = !prev;
      if (next && isCompactViewport) setLeftPanelOpen(false);
      return next;
    });
  };

  return (
    <div className="studio-shell">
      <header className="topbar">
        <div className="topbar-group">
          <button
            type="button"
            className="btn btn-ghost btn-icon"
            onClick={openLeftDrawer}
            title="Toggle Node Library"
            aria-label="Toggle Node Library"
          >
            <PanelLeft size={16} />
          </button>

          <div className="brand">
            <span className="brand-mark">FP</span>
            <span className="brand-text">FlowPilot Studio</span>
          </div>

          <span className="topbar-divider tablet-up" />

          {savedWorkflows.length > 0 && (
            <select
              className="select workflow-switcher"
              value={workflowRecord?.id ?? ""}
              onChange={(e) => {
                const found = savedWorkflows.find((w) => w.id === e.target.value);
                if (found) {
                  setWorkflowRecord(found);
                  setWorkflowName(found.name);
                  setGraph(found.definition.graph);
                  setStatusBanner(`Loaded '${found.name}' (r${found.revision})`);
                }
              }}
            >
              <option value="">Switch workflow...</option>
              {savedWorkflows.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name} (r{w.revision})
                </option>
              ))}
            </select>
          )}

          <input
            className="workflow-title-input"
            aria-label="Workflow name"
            value={workflowName}
            onChange={(e) => setWorkflowName(e.target.value)}
          />

          <span className="pill tablet-up">
            {workflowRecord ? `r${workflowRecord.revision}` : "draft"}
          </span>
          {publishedVersion !== null && (
            <span className="pill pill-success tablet-up">v{publishedVersion}</span>
          )}
        </div>

        <div className="topbar-group">
          <button
            type="button"
            className="btn btn-ghost tablet-up"
            onClick={handleOpenTemplates}
            title="Templates Gallery"
          >
            <LayoutTemplate size={14} /> Templates
          </button>
          <button
            type="button"
            className="btn btn-ghost tablet-up"
            onClick={handleOpenInbox}
            title="Approvals Inbox"
          >
            <Inbox size={14} /> Inbox
          </button>
          <button
            type="button"
            className="btn btn-ghost tablet-up"
            onClick={() => setAiModalOpen(true)}
            title="Generate workflow draft with AI"
          >
            <Sparkles size={14} color="var(--brand-accent)" /> AI Draft
          </button>

          <span className="topbar-divider desktop-only" />

          <button
            type="button"
            className="btn btn-ghost desktop-only"
            onClick={handleValidate}
            title="Validate graph & policies"
          >
            <CheckSquare size={14} /> Validate
          </button>
          <button
            type="button"
            className="btn btn-ghost desktop-only"
            onClick={handleSave}
            title="Save draft"
          >
            <Save size={14} /> Save
          </button>

          <button type="button" className="btn btn-primary" onClick={handlePublish}>
            <Rocket size={13} /> <span className="btn-label-hide-xs">Publish</span>
          </button>
          <button type="button" className="btn btn-success" onClick={handleRun}>
            <Play size={13} /> <span className="btn-label-hide-xs">Run</span>
          </button>

          <div className="menu-anchor" ref={menuRef}>
            <button
              type="button"
              className="btn btn-ghost btn-icon"
              onClick={() => setMoreMenuOpen((v) => !v)}
              title="More actions"
              aria-label="More actions"
              aria-expanded={moreMenuOpen}
            >
              <MoreHorizontal size={16} />
            </button>

            {moreMenuOpen && (
              <div className="menu-dropdown" role="menu">
                <button
                  type="button"
                  className="menu-item"
                  onClick={() => {
                    setMoreMenuOpen(false);
                    handleNewWorkflow();
                  }}
                >
                  <Plus size={14} /> New Workflow
                </button>
                <button
                  type="button"
                  className="menu-item"
                  onClick={() => {
                    setMoreMenuOpen(false);
                    void handleOpenTemplates();
                  }}
                >
                  <LayoutTemplate size={14} /> Templates
                </button>
                <button
                  type="button"
                  className="menu-item"
                  onClick={() => {
                    setMoreMenuOpen(false);
                    void handleOpenInbox();
                  }}
                >
                  <Inbox size={14} /> Approvals Inbox
                </button>
                <button
                  type="button"
                  className="menu-item"
                  onClick={() => {
                    setMoreMenuOpen(false);
                    setAiModalOpen(true);
                  }}
                >
                  <Sparkles size={14} /> AI Draft
                </button>
                <div className="menu-sep" />
                <button
                  type="button"
                  className="menu-item"
                  onClick={() => {
                    setMoreMenuOpen(false);
                    void handleValidate();
                    if (isCompactViewport) setRightPanelOpen(true);
                  }}
                >
                  <CheckSquare size={14} /> Validate
                </button>
                <button
                  type="button"
                  className="menu-item"
                  onClick={() => {
                    setMoreMenuOpen(false);
                    void handleSave();
                  }}
                >
                  <Save size={14} /> Save Draft
                </button>
                <button
                  type="button"
                  className="menu-item"
                  onClick={() => {
                    setMoreMenuOpen(false);
                    void handleOpenVersionHistory();
                  }}
                >
                  <GitCommitHorizontal size={14} /> Versions &amp; Diff
                </button>
                <div className="menu-sep" />
                <button
                  type="button"
                  className="menu-item"
                  onClick={() => {
                    setMoreMenuOpen(false);
                    commitGraph((g) => autoLayoutGraph(g));
                  }}
                >
                  <LayoutGrid size={14} /> Auto Layout
                </button>
                <button
                  type="button"
                  className="menu-item"
                  disabled={undoStack.length === 0}
                  onClick={() => {
                    setMoreMenuOpen(false);
                    handleUndo();
                  }}
                >
                  <Undo2 size={14} /> Undo
                </button>
                <button
                  type="button"
                  className="menu-item"
                  disabled={redoStack.length === 0}
                  onClick={() => {
                    setMoreMenuOpen(false);
                    handleRedo();
                  }}
                >
                  <Redo2 size={14} /> Redo
                </button>
                <button
                  type="button"
                  className="menu-item"
                  onClick={() => {
                    setMoreMenuOpen(false);
                    setAssistantOpen(true);
                  }}
                >
                  <Bot size={14} /> AI Assistant
                </button>
                <button
                  type="button"
                  className="menu-item"
                  onClick={async () => {
                    setMoreMenuOpen(false);
                    try {
                      await ensureSession();
                    } catch {
                      // offline fallback
                    }
                    setHubTab("insights");
                    setHubModalOpen(true);
                  }}
                >
                  <BarChart3 size={14} /> Insights &amp; Hub
                </button>
                <button
                  type="button"
                  className="menu-item"
                  onClick={async () => {
                    setMoreMenuOpen(false);
                    try {
                      await ensureSession();
                    } catch {
                      // offline fallback
                    }
                    setHubTab("tables");
                    setHubModalOpen(true);
                  }}
                >
                  <Database size={14} /> Data Tables &amp; RAG
                </button>
                {workflowRecord && (
                  <>
                    <button
                      type="button"
                      className="menu-item"
                      onClick={() => {
                        setMoreMenuOpen(false);
                        window.open(`/chat/${workflowRecord.id}`, "_blank", "noopener");
                      }}
                    >
                      <MessageSquare size={14} /> Open Hosted Chat
                    </button>
                    <button
                      type="button"
                      className="menu-item"
                      onClick={() => {
                        setMoreMenuOpen(false);
                        const snippet = buildChatEmbedSnippet(workflowRecord.id);
                        if (typeof navigator !== "undefined" && navigator.clipboard) {
                          void navigator.clipboard.writeText(snippet).catch(() => undefined);
                        }
                        setStatusBanner("Copied embeddable ChatHub <script> snippet");
                      }}
                    >
                      <MessageSquare size={14} /> Copy Embed Widget
                    </button>
                  </>
                )}
                <button
                  type="button"
                  className="menu-item"
                  onClick={() => {
                    setMoreMenuOpen(false);
                    setAutosaveEnabled((prev) => {
                      const next = !prev;
                      setStatusBanner(next ? "Autosave & Edit Lock enabled" : "Autosave disabled");
                      return next;
                    });
                  }}
                >
                  <Save size={14} /> {autosaveEnabled ? "Autosave: ON" : "Autosave: OFF"}
                </button>
                <div className="menu-sep" />
                <button
                  type="button"
                  className="menu-item"
                  onClick={() => {
                    setMoreMenuOpen(false);
                    const blob = new Blob(
                      [JSON.stringify({ name: workflowName, graph }, null, 2)],
                      { type: "application/json" },
                    );
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement("a");
                    a.href = url;
                    a.download = `${workflowName.toLowerCase().replace(/[^a-z0-9]+/g, "-") || "workflow"}.flowpilot.json`;
                    a.click();
                    URL.revokeObjectURL(url);
                    setStatusBanner("Exported workflow JSON");
                  }}
                >
                  <Download size={14} /> Export JSON
                </button>
                <button
                  type="button"
                  className="menu-item"
                  onClick={() => {
                    setMoreMenuOpen(false);
                    importFileRef.current?.click();
                  }}
                >
                  <Upload size={14} /> Import JSON
                </button>
                <div className="menu-sep" />
                <button
                  type="button"
                  className="menu-item"
                  onClick={() => {
                    setMoreMenuOpen(false);
                    toggleTheme();
                  }}
                >
                  {theme === "light" ? <Moon size={14} /> : <Sun size={14} />}
                  {theme === "light" ? "Dark Mode" : "Light Mode"}
                </button>
                <button
                  type="button"
                  className="menu-item"
                  onClick={() => {
                    setMoreMenuOpen(false);
                    setAuthModalOpen(true);
                  }}
                >
                  <LogIn size={14} /> {session ? "Account" : "Sign In"}
                </button>
              </div>
            )}
          </div>

          <button
            type="button"
            className="btn btn-ghost btn-icon"
            onClick={openRightDrawer}
            title="Toggle Inspector"
            aria-label="Toggle Inspector"
          >
            <PanelRight size={16} />
          </button>
        </div>
      </header>

      <div className="workspace-body">
        {isCompactViewport && (leftPanelOpen || rightPanelOpen) && (
          <div
            className="drawer-backdrop"
            onClick={() => {
              setLeftPanelOpen(false);
              setRightPanelOpen(false);
            }}
          />
        )}

        <NodeLibrary
          catalog={catalog}
          open={leftPanelOpen}
          onClose={() => setLeftPanelOpen(false)}
          onAddNode={(entry) => {
            handleAddNode(entry);
            if (isCompactViewport) setLeftPanelOpen(false);
          }}
        />

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
              if (isCompactViewport) setRightPanelOpen(true);
            }}
            onEdgeClick={(_, edge) => {
              setSelectedEdgeId(edge.id);
              setSelectedNodeId(null);
              setActiveTab("configure");
              if (isCompactViewport) setRightPanelOpen(true);
            }}
            onPaneClick={() => {
              setSelectedNodeId(null);
              setSelectedEdgeId(null);
            }}
            fitView
            colorMode={theme}
          >
            <Background
              variant={BackgroundVariant.Dots}
              color={theme === "dark" ? "#27272a" : "#d4d4d8"}
              gap={22}
              size={1.2}
            />
            <Controls showInteractive={false} />
            <MiniMap
              nodeColor={theme === "dark" ? "#52525b" : "#a1a1aa"}
              maskColor={theme === "dark" ? "rgba(9, 9, 11, 0.72)" : "rgba(250, 250, 250, 0.72)"}
            />
          </ReactFlow>

          <div className="canvas-status-pill" role="status" aria-live="polite">
            <span
              className="status-dot"
              style={{ background: session ? "var(--success)" : "var(--text-muted)" }}
            />
            <span>
              {statusBanner}
              {lockOwner ? ` · lock:${lockOwner.slice(0, 14)}` : ""}
            </span>
          </div>
        </main>

        <InspectorPanel
          open={rightPanelOpen}
          onClose={() => setRightPanelOpen(false)}
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
          onTestNode={async (nodeDef) => {
            const sess = await ensureSession();
            let parsedTrigger: Record<string, unknown> = {};
            try {
              parsedTrigger = JSON.parse(triggerInputJson || "{}");
            } catch {
              parsedTrigger = {};
            }
            const res = await testSingleNodeApi(sess.api_key, {
              node_id: nodeDef.id,
              node_type: nodeDef.type,
              config: nodeDef.config,
              credentials: nodeDef.credentials,
              pinned_nodes: pinnedOutputs,
              trigger: parsedTrigger,
            });
            setStatusBanner(
              res.ok
                ? `Tested node '${nodeDef.id}' in ${res.duration_ms}ms`
                : `Test failed on '${nodeDef.id}': ${res.error}`,
            );
            return res;
          }}
          onReplayFromNode={handleReplayFromNode}
          pinnedOutputs={pinnedOutputs}
          onPinNodeOutput={(nid, out) => {
            setPinnedOutputs((prev) => ({ ...prev, [nid]: out }));
            setStatusBanner(`Pinned sample output for node '${nid}'`);
          }}
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
        diffResult={diffResult}
        onClose={() => setVersionModalOpen(false)}
        onCompare={handleCompareVersions}
        onRollback={handleRollbackVersion}
      />

      <TemplatesModal
        open={templatesModalOpen}
        templates={templates}
        onClose={() => setTemplatesModalOpen(false)}
        onSelectTemplate={(tpl) => {
          setWorkflowRecord(null);
          setPublishedVersion(null);
          setWorkflowName(tpl.name);
          commitGraph(() => tpl.graph);
          setTemplatesModalOpen(false);
          setStatusBanner(`Loaded template '${tpl.name}' onto canvas`);
        }}
      />

      <ApprovalInboxModal
        open={inboxModalOpen}
        items={inboxItems}
        onClose={() => setInboxModalOpen(false)}
        onDecide={async (id, approved, comment) => {
          await handleDecideApproval(id, approved, comment);
          if (session) setInboxItems(await listApprovalsInboxApi(session.api_key));
        }}
        onGenerateLinks={async (id) => {
          if (!session) throw new Error("Sign in first");
          return generateApprovalActionLinksApi(session.api_key, id);
        }}
      />

      <WorkspaceHubModal
        open={hubModalOpen}
        initialTab={hubTab}
        apiKey={session?.api_key ?? null}
        projectId={session?.project_id ?? null}
        onClose={() => setHubModalOpen(false)}
        onConnectorsImported={async () => {
          const updatedCatalog = await fetchNodeCatalog();
          if (updatedCatalog.length > 0) setCatalog(updatedCatalog);
        }}
      />

      <AiAssistantModal
        open={assistantOpen}
        loading={assistantLoading}
        onClose={() => setAssistantOpen(false)}
        onSend={async (msg) => {
          setAssistantLoading(true);
          try {
            const sess = await ensureSession();
            const res = await runAiAssistantApi(sess.api_key, {
              message: msg,
              graph,
              workflow_name: workflowName,
            });
            if (res.modified) {
              commitGraph(() => res.graph);
              setValidation(res.validation);
              setStatusBanner("AI Assistant updated canvas graph");
            }
            return res.reply;
          } finally {
            setAssistantLoading(false);
          }
        }}
      />

      <input
        ref={importFileRef}
        type="file"
        accept="application/json,.json"
        style={{ display: "none" }}
        onChange={async (e) => {
          const file = e.target.files?.[0];
          if (!file) return;
          try {
            const parsed = JSON.parse(await file.text());
            const importedGraph: WorkflowGraph = parsed.graph ?? parsed.definition?.graph;
            if (!importedGraph?.nodes) throw new Error("Invalid workflow JSON");
            if (parsed.name) setWorkflowName(String(parsed.name));
            commitGraph(() => importedGraph);
            setStatusBanner(`Imported '${file.name}' (${importedGraph.nodes.length} nodes)`);
          } catch (err) {
            setStatusBanner(`Import failed: ${err instanceof Error ? err.message : String(err)}`);
          } finally {
            e.target.value = "";
          }
        }}
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
