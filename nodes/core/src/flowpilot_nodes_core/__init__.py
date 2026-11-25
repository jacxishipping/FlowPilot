"""First-party core node package registration and manifest."""

from __future__ import annotations

from typing import Any

from flowpilot import BaseNode, NodePackageManifest, NodeRegistry
from flowpilot_nodes_core.ai import (
    AgentToolCall,
    AiPromptConfig,
    AiPromptNode,
    BoundedAgentConfig,
    BoundedAgentNode,
    ModelCompletion,
    ModelProvider,
    ToolHandler,
)
from flowpilot_nodes_core.approval import HumanApprovalConfig, HumanApprovalNode
from flowpilot_nodes_core.control import (
    ConditionConfig,
    ConditionNode,
    DelayConfig,
    DelayNode,
    ErrorHandlerConfig,
    ErrorHandlerNode,
    LoopNode,
)
from flowpilot_nodes_core.data import SetDataConfig, SetDataNode, TransformConfig, TransformNode
from flowpilot_nodes_core.http_node import HttpRequestConfig, HttpRequestNode
from flowpilot_nodes_core.logic import (
    EXTENDED_CORE_NODES,
    AggregateConfig,
    AggregateNode,
    ChatTriggerConfig,
    ChatTriggerNode,
    DataTableConfig,
    DataTableNode,
    DataTableServiceFn,
    ErrorTriggerConfig,
    ErrorTriggerNode,
    FilterConfig,
    FilterNode,
    GraphQLRequestConfig,
    GraphQLRequestNode,
    JavaScriptCodeConfig,
    JavaScriptCodeNode,
    KnowledgeSearchConfig,
    KnowledgeSearchFn,
    KnowledgeSearchNode,
    MergeConfig,
    MergeNode,
    SortConfig,
    SortNode,
    SplitInBatchesConfig,
    SplitInBatchesNode,
    SwitchConfig,
    SwitchNode,
)
from flowpilot_nodes_core.subworkflow import (
    MAX_SUBWORKFLOW_DEPTH,
    SubWorkflowConfig,
    SubWorkflowNode,
    SubWorkflowRunnerFn,
    WebhookResponseConfig,
    WebhookResponseNode,
)
from flowpilot_nodes_core.triggers import (
    ManualTriggerConfig,
    ManualTriggerNode,
    WebhookTriggerConfig,
    WebhookTriggerNode,
)

CORE_NODES: tuple[type[BaseNode[Any, Any]], ...] = (
    ManualTriggerNode,
    WebhookTriggerNode,
    SetDataNode,
    TransformNode,
    ConditionNode,
    LoopNode,
    DelayNode,
    ErrorHandlerNode,
    HttpRequestNode,
    HumanApprovalNode,
    AiPromptNode,
    BoundedAgentNode,
    SubWorkflowNode,
    WebhookResponseNode,
    *EXTENDED_CORE_NODES,
)

CORE_PACKAGE_MANIFEST = NodePackageManifest(
    package_name="flowpilot-nodes-core",
    package_version="0.1.0",
    sdk_compatibility=">=0.1.0,<1.0.0",
    first_party=True,
    entrypoints={
        node_cls.metadata.type: f"{node_cls.__module__}:{node_cls.__name__}"
        for node_cls in CORE_NODES
    },
    permissions=("http.outbound", "ai.invoke", "ai.agent.run", "code.execute"),
    credential_types=("http_auth",),
)


def register_core_nodes(registry: NodeRegistry, *, replace: bool = False) -> NodeRegistry:
    """Register all first-party core nodes into the given NodeRegistry."""
    for node_cls in CORE_NODES:
        registry.register(node_cls, replace=replace)
    return registry


def create_default_registry() -> NodeRegistry:
    """Create a fresh NodeRegistry populated with all first-party core nodes."""
    return register_core_nodes(NodeRegistry())


__all__ = [
    "CORE_NODES",
    "CORE_PACKAGE_MANIFEST",
    "EXTENDED_CORE_NODES",
    "MAX_SUBWORKFLOW_DEPTH",
    "AgentToolCall",
    "AggregateConfig",
    "AggregateNode",
    "AiPromptConfig",
    "AiPromptNode",
    "BoundedAgentConfig",
    "BoundedAgentNode",
    "ChatTriggerConfig",
    "ChatTriggerNode",
    "ConditionConfig",
    "ConditionNode",
    "DataTableConfig",
    "DataTableNode",
    "DataTableServiceFn",
    "DelayConfig",
    "DelayNode",
    "ErrorHandlerConfig",
    "ErrorHandlerNode",
    "ErrorTriggerConfig",
    "ErrorTriggerNode",
    "FilterConfig",
    "FilterNode",
    "GraphQLRequestConfig",
    "GraphQLRequestNode",
    "HttpRequestConfig",
    "HttpRequestNode",
    "HumanApprovalConfig",
    "HumanApprovalNode",
    "JavaScriptCodeConfig",
    "JavaScriptCodeNode",
    "KnowledgeSearchConfig",
    "KnowledgeSearchFn",
    "KnowledgeSearchNode",
    "LoopNode",
    "ManualTriggerConfig",
    "ManualTriggerNode",
    "MergeConfig",
    "MergeNode",
    "ModelCompletion",
    "ModelProvider",
    "SetDataConfig",
    "SetDataNode",
    "SortConfig",
    "SortNode",
    "SplitInBatchesConfig",
    "SplitInBatchesNode",
    "SubWorkflowConfig",
    "SubWorkflowNode",
    "SubWorkflowRunnerFn",
    "SwitchConfig",
    "SwitchNode",
    "ToolHandler",
    "TransformConfig",
    "TransformNode",
    "WebhookResponseConfig",
    "WebhookResponseNode",
    "WebhookTriggerConfig",
    "WebhookTriggerNode",
    "create_default_registry",
    "register_core_nodes",
]
