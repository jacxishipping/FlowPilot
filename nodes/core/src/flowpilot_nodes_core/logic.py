"""Extended core workflow nodes: Switch, Merge, SplitInBatches, Filter, Sort, Aggregate,
GraphQL, Sandboxed JavaScript, Chat Trigger, Error Trigger, Data Table, and Knowledge RAG."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar, Literal
from uuid import UUID

from pydantic import Field

from flowpilot import (
    BaseNode,
    ConfigurationError,
    CredentialError,
    CredentialRequirement,
    ExecutionContext,
    NodeConfigModel,
    NodeMetadata,
    NodeResult,
    NonRetryableNodeError,
    RetryableNodeError,
    SecurityPolicyError,
)
from flowpilot_schema import NodeKind, RiskLevel, SideEffectPolicy, WorkerPool


# 1. Switch (multi-branch routing)
class SwitchConfig(NodeConfigModel):
    value: Any = Field(..., description="Value to compare against route match values.")
    routes: dict[str, Any] = Field(
        default_factory=dict,
        description="Map of branch_name -> expected value.",
    )
    default_branch: str = Field(default="default", min_length=1, max_length=64)


class SwitchNode(BaseNode[SwitchConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="core.switch",
        name="Switch",
        description="Routes execution down a named branch matching a value or default fallback.",
        category="control",
        kind=NodeKind.CONTROL,
        worker_pool=WorkerPool.WORKFLOW,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.LOW,
    )
    config_model: ClassVar[type[SwitchConfig]] = SwitchConfig

    async def execute(self, config: SwitchConfig, ctx: ExecutionContext) -> NodeResult:
        matched = config.default_branch
        for branch_name, expected in config.routes.items():
            if config.value == expected:
                matched = branch_name
                break
        return NodeResult(
            output={"value": config.value, "branch": matched},
            branch=matched,
        )


# 2. Merge
class MergeConfig(NodeConfigModel):
    mode: Literal["append", "combine_objects", "zip"] = "append"
    inputs: list[Any] = Field(default_factory=list)


class MergeNode(BaseNode[MergeConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="core.merge",
        name="Merge",
        description="Combines multiple upstream lists or objects using append, merge, or zip.",
        category="data",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.WORKFLOW,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.LOW,
    )
    config_model: ClassVar[type[MergeConfig]] = MergeConfig

    async def execute(self, config: MergeConfig, ctx: ExecutionContext) -> NodeResult:
        if config.mode == "combine_objects":
            merged: dict[str, Any] = {}
            for item in config.inputs:
                if isinstance(item, dict):
                    merged.update(item)
            return NodeResult(output={"merged": merged})
        if config.mode == "zip":
            lists = [x if isinstance(x, list) else [x] for x in config.inputs]
            zipped = [list(tup) for tup in zip(*lists, strict=False)] if lists else []
            return NodeResult(output={"items": zipped, "count": len(zipped)})
        out: list[Any] = []
        for item in config.inputs:
            if isinstance(item, list):
                out.extend(item)
            else:
                out.append(item)
        return NodeResult(output={"items": out, "count": len(out)})


# 3. Split in Batches
class SplitInBatchesConfig(NodeConfigModel):
    items: list[Any] = Field(default_factory=list)
    batch_size: int = Field(default=10, ge=1, le=5000)


class SplitInBatchesNode(BaseNode[SplitInBatchesConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="core.split_in_batches",
        name="Split in Batches",
        description="Chunks an array into fixed-size batches for rate-limited downstream calls.",
        category="data",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.WORKFLOW,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.LOW,
    )
    config_model: ClassVar[type[SplitInBatchesConfig]] = SplitInBatchesConfig

    async def execute(self, config: SplitInBatchesConfig, ctx: ExecutionContext) -> NodeResult:
        sz = config.batch_size
        batches = [config.items[i : i + sz] for i in range(0, len(config.items), sz)]
        return NodeResult(
            output={
                "batches": batches,
                "batch_count": len(batches),
                "total_items": len(config.items),
            }
        )


# 4. Filter
class FilterConfig(NodeConfigModel):
    items: list[Any] = Field(default_factory=list)
    field: str = Field(..., min_length=1, max_length=128)
    operator: Literal["eq", "neq", "gt", "gte", "lt", "lte", "contains", "in"] = "eq"
    value: Any = None


def _get_nested(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


class FilterNode(BaseNode[FilterConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="core.filter",
        name="Filter",
        description="Filters array items by field comparison into matched and rejected lists.",
        category="data",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.WORKFLOW,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.LOW,
    )
    config_model: ClassVar[type[FilterConfig]] = FilterConfig

    async def execute(self, config: FilterConfig, ctx: ExecutionContext) -> NodeResult:
        matched: list[Any] = []
        rejected: list[Any] = []
        op = config.operator
        target = config.value

        for item in config.items:
            val = _get_nested(item, config.field)
            ok = False
            if op == "eq":
                ok = val == target
            elif op == "neq":
                ok = val != target
            elif op == "gt":
                ok = val is not None and target is not None and val > target
            elif op == "gte":
                ok = val is not None and target is not None and val >= target
            elif op == "lt":
                ok = val is not None and target is not None and val < target
            elif op == "lte":
                ok = val is not None and target is not None and val <= target
            elif op == "contains":
                ok = target in val if isinstance(val, (str, list, dict)) else False
            elif op == "in":
                ok = val in target if isinstance(target, (list, tuple, set, str)) else False
            (matched if ok else rejected).append(item)

        return NodeResult(
            output={
                "items": matched,
                "rejected": rejected,
                "matched_count": len(matched),
                "rejected_count": len(rejected),
            }
        )


# 5. Sort
class SortConfig(NodeConfigModel):
    items: list[Any] = Field(default_factory=list)
    field: str = Field(..., min_length=1, max_length=128)
    descending: bool = False


class SortNode(BaseNode[SortConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="core.sort",
        name="Sort",
        description="Sorts an array of objects ascending or descending by a field path.",
        category="data",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.WORKFLOW,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.LOW,
    )
    config_model: ClassVar[type[SortConfig]] = SortConfig

    async def execute(self, config: SortConfig, ctx: ExecutionContext) -> NodeResult:
        sorted_items = sorted(
            config.items,
            key=lambda x: (_get_nested(x, config.field) is None, _get_nested(x, config.field)),
            reverse=config.descending,
        )
        return NodeResult(output={"items": sorted_items, "count": len(sorted_items)})


# 6. Aggregate
class AggregateConfig(NodeConfigModel):
    items: list[Any] = Field(default_factory=list)
    field: str | None = None
    group_by: str | None = None
    operation: Literal["count", "sum", "avg", "min", "max"] = "count"


def _agg_values(items: list[Any], field: str | None, op: str) -> float | int | None:
    if op == "count":
        return len(items)
    nums: list[float] = []
    for it in items:
        v = _get_nested(it, field) if field else it
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            nums.append(float(v))
    if not nums:
        return 0 if op == "sum" else None
    if op == "sum":
        return sum(nums)
    if op == "avg":
        return sum(nums) / len(nums)
    if op == "min":
        return min(nums)
    return max(nums)


class AggregateNode(BaseNode[AggregateConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="core.aggregate",
        name="Aggregate",
        description="Computes count, sum, avg, min, or max over arrays with optional group_by.",
        category="data",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.WORKFLOW,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.LOW,
    )
    config_model: ClassVar[type[AggregateConfig]] = AggregateConfig

    async def execute(self, config: AggregateConfig, ctx: ExecutionContext) -> NodeResult:
        if config.group_by:
            groups: dict[str, list[Any]] = defaultdict(list)
            for item in config.items:
                key = str(_get_nested(item, config.group_by))
                groups[key].append(item)
            summary = {
                k: _agg_values(v, config.field, config.operation) for k, v in groups.items()
            }
            return NodeResult(output={"groups": summary, "group_count": len(summary)})
        value = _agg_values(config.items, config.field, config.operation)
        return NodeResult(output={"value": value, "operation": config.operation})


# 7. GraphQL Request
class GraphQLRequestConfig(NodeConfigModel):
    endpoint: str = Field(..., min_length=8, max_length=2048)
    query: str = Field(..., min_length=1)
    variables: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    operation_name: str | None = None


class GraphQLRequestNode(BaseNode[GraphQLRequestConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="core.graphql_request",
        name="GraphQL Request",
        description="Executes an SSRF-protected GraphQL query or mutation with optional auth.",
        category="http",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.INTEGRATIONS,
        side_effect=SideEffectPolicy.IDEMPOTENT,
        risk_level=RiskLevel.MEDIUM,
        credentials=(
            CredentialRequirement(slot="auth", credential_type="http_auth", required=False),
        ),
        required_permissions=("http.outbound",),
    )
    config_model: ClassVar[type[GraphQLRequestConfig]] = GraphQLRequestConfig

    async def execute(self, config: GraphQLRequestConfig, ctx: ExecutionContext) -> NodeResult:
        headers = {"Content-Type": "application/json", **config.headers}
        try:
            cred = await ctx.get_credential("auth")
            if "token" in cred.secrets:
                headers["Authorization"] = f"Bearer {cred.get_secret('token')}"
        except CredentialError:
            pass
        body: dict[str, Any] = {"query": config.query, "variables": config.variables}
        if config.operation_name:
            body["operationName"] = config.operation_name
        resp = await ctx.http.request("POST", config.endpoint, headers=headers, json_body=body)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise RetryableNodeError(f"GraphQL transient HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise NonRetryableNodeError(f"GraphQL error HTTP {resp.status_code}")
        payload = resp.body_json if isinstance(resp.body_json, dict) else {}
        return NodeResult(
            output={
                "data": payload.get("data"),
                "errors": payload.get("errors"),
                "status_code": resp.status_code,
            }
        )


# 8. Sandboxed JavaScript Code Node
class JavaScriptCodeConfig(NodeConfigModel):
    code: str = Field(..., min_length=1, max_length=32_000)
    input_data: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=5.0, ge=0.5, le=30.0)


_JS_SANDBOX_HARNESS = r"""
const vm = require('vm');
let raw = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { raw += chunk; });
process.stdin.on('end', () => {
  try {
    const req = JSON.parse(raw);
    const logs = [];
    const sandbox = Object.create(null);
    sandbox.input = req.input;
    sandbox.JSON = JSON;
    sandbox.Math = Math;
    sandbox.Date = Date;
    sandbox.Array = Array;
    sandbox.Object = Object;
    sandbox.String = String;
    sandbox.Number = Number;
    sandbox.Boolean = Boolean;
    sandbox.RegExp = RegExp;
    sandbox.console = { log: (...a) => logs.push(a.map(String).join(' ')) };
    const ctx = vm.createContext(sandbox, { codeGeneration: { strings: false, wasm: false } });
    const wrapped = `(function(input) {\n${req.code}\n})(input)`;
    const result = vm.runInContext(wrapped, ctx, { timeout: req.timeout_ms });
    const out = { ok: true, result: result === undefined ? null : result, logs };
    process.stdout.write(JSON.stringify(out));
  } catch (err) {
    process.stdout.write(JSON.stringify({ ok: false, error: String(err && err.message || err) }));
  }
});
"""


class JavaScriptCodeNode(BaseNode[JavaScriptCodeConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="core.js_code",
        name="JavaScript Code",
        description="Runs sandboxed JavaScript (node:vm without fs/net/env) with strict timeout.",
        category="data",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.CODE,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.MEDIUM,
        required_permissions=("code.execute",),
    )
    config_model: ClassVar[type[JavaScriptCodeConfig]] = JavaScriptCodeConfig

    async def execute(self, config: JavaScriptCodeConfig, ctx: ExecutionContext) -> NodeResult:
        node_bin = shutil.which("node")
        if not node_bin:
            raise ConfigurationError("Node.js runtime not found on worker PATH for core.js_code.")
        proc = await asyncio.create_subprocess_exec(
            node_bin,
            "-e",
            _JS_SANDBOX_HARNESS,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        req_bytes = json.dumps(
            {
                "code": config.code,
                "input": config.input_data,
                "timeout_ms": int(config.timeout_seconds * 1000),
            }
        ).encode("utf-8")
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(req_bytes),
                timeout=config.timeout_seconds + 1.0,
            )
        except TimeoutError as exc:
            proc.kill()
            raise SecurityPolicyError(
                f"JavaScript sandbox exceeded {config.timeout_seconds}s timeout.",
                code="JS_SANDBOX_TIMEOUT",
            ) from exc

        parsed = json.loads(stdout.decode("utf-8") or '{"ok":false,"error":"empty output"}')
        if not parsed.get("ok"):
            raise NonRetryableNodeError(
                f"JavaScript execution failed: {parsed.get('error')}",
                code="JS_EXECUTION_ERROR",
            )
        return NodeResult(output={"result": parsed.get("result"), "logs": parsed.get("logs", [])})


# 9. Chat Trigger
class ChatTriggerConfig(NodeConfigModel):
    welcome_message: str = Field(default="Hi! How can I help you today?")
    system_prompt: str = ""


class ChatTriggerNode(BaseNode[ChatTriggerConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="core.chat_trigger",
        name="Chat Trigger",
        description="Starts a conversational workflow from hosted or embedded ChatHub messages.",
        category="triggers",
        kind=NodeKind.TRIGGER,
        worker_pool=WorkerPool.WORKFLOW,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.LOW,
    )
    config_model: ClassVar[type[ChatTriggerConfig]] = ChatTriggerConfig

    async def execute(self, config: ChatTriggerConfig, ctx: ExecutionContext) -> NodeResult:
        return NodeResult(
            output={
                "session_id": ctx.trigger_payload.get("session_id", ""),
                "message": ctx.trigger_payload.get("message", ""),
                "history": ctx.trigger_payload.get("history", []),
                "welcome_message": config.welcome_message,
            }
        )


# 10. Error Trigger
class ErrorTriggerConfig(NodeConfigModel):
    capture_stack: bool = True


class ErrorTriggerNode(BaseNode[ErrorTriggerConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="core.error_trigger",
        name="Error Trigger",
        description="Triggered automatically when a linked workflow fails with error context.",
        category="triggers",
        kind=NodeKind.TRIGGER,
        worker_pool=WorkerPool.WORKFLOW,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.LOW,
    )
    config_model: ClassVar[type[ErrorTriggerConfig]] = ErrorTriggerConfig

    async def execute(self, config: ErrorTriggerConfig, ctx: ExecutionContext) -> NodeResult:
        return NodeResult(
            output={
                "failed_workflow_id": ctx.trigger_payload.get("failed_workflow_id"),
                "failed_run_id": ctx.trigger_payload.get("failed_run_id"),
                "error_message": ctx.trigger_payload.get("error_message", ""),
                "failed_node_id": ctx.trigger_payload.get("failed_node_id"),
            }
        )


# 11. Built-In Data Table Node
DataTableServiceFn = Callable[
    [UUID, str, str, dict[str, Any] | None, str | None],
    Awaitable[dict[str, Any]],
]


class DataTableConfig(NodeConfigModel):
    table_name: str = Field(..., min_length=1, max_length=128)
    operation: Literal["list", "insert", "update", "delete"] = "list"
    row_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class DataTableNode(BaseNode[DataTableConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="core.data_table",
        name="Data Table",
        description="Reads or writes rows in FlowPilot's built-in tenant-isolated Data Tables.",
        category="data",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.WORKFLOW,
        side_effect=SideEffectPolicy.IDEMPOTENT,
        risk_level=RiskLevel.LOW,
    )
    config_model: ClassVar[type[DataTableConfig]] = DataTableConfig

    async def execute(self, config: DataTableConfig, ctx: ExecutionContext) -> NodeResult:
        svc: DataTableServiceFn | None = ctx.services.get("data_table_service")
        if svc is None:
            raise ConfigurationError(
                "No 'data_table_service' configured in ExecutionContext.services."
            )
        out = await svc(
            ctx.organization_id,
            config.table_name,
            config.operation,
            config.data,
            config.row_id,
        )
        return NodeResult(output=out)


# 12. Knowledge Base / RAG Search Node
KnowledgeSearchFn = Callable[[UUID, str, int, str | None], Awaitable[list[dict[str, Any]]]]


class KnowledgeSearchConfig(NodeConfigModel):
    query: str = Field(..., min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=50)
    collection: str | None = None


class KnowledgeSearchNode(BaseNode[KnowledgeSearchConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="core.knowledge_search",
        name="Knowledge Search (RAG)",
        description="Retrieves top-K relevant document chunks from the workspace Knowledge Base.",
        category="ai",
        kind=NodeKind.AI,
        worker_pool=WorkerPool.AGENTS,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.LOW,
    )
    config_model: ClassVar[type[KnowledgeSearchConfig]] = KnowledgeSearchConfig

    async def execute(self, config: KnowledgeSearchConfig, ctx: ExecutionContext) -> NodeResult:
        search_fn: KnowledgeSearchFn | None = ctx.services.get("knowledge_search")
        if search_fn is None:
            raise ConfigurationError(
                "No 'knowledge_search' configured in ExecutionContext.services."
            )
        chunks = await search_fn(ctx.organization_id, config.query, config.top_k, config.collection)
        context_text = "\n\n".join(str(c.get("content", "")) for c in chunks)
        return NodeResult(
            output={
                "query": config.query,
                "chunks": chunks,
                "count": len(chunks),
                "context": context_text,
            }
        )


EXTENDED_CORE_NODES: tuple[type[BaseNode[Any, Any]], ...] = (
    SwitchNode,
    MergeNode,
    SplitInBatchesNode,
    FilterNode,
    SortNode,
    AggregateNode,
    GraphQLRequestNode,
    JavaScriptCodeNode,
    ChatTriggerNode,
    ErrorTriggerNode,
    DataTableNode,
    KnowledgeSearchNode,
)
