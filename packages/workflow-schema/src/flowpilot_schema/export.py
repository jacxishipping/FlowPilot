"""Canonical JSON serialization, hashing, and JSON Schema export."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel

from flowpilot_schema.graph import WorkflowDefinition, WorkflowGraph


def _strip_ui_from_graph(graph_dict: dict[str, Any]) -> dict[str, Any]:
    """Remove UI-only metadata so semantic hashes are invariant to canvas dragging."""
    cleaned_nodes = []
    for node in graph_dict.get("nodes", []):
        node_copy = {k: v for k, v in node.items() if k != "ui"}
        loop = node_copy.get("loop")
        if isinstance(loop, dict) and isinstance(loop.get("body_graph"), dict):
            loop_copy = dict(loop)
            loop_copy["body_graph"] = _strip_ui_from_graph(loop_copy["body_graph"])
            node_copy["loop"] = loop_copy
        cleaned_nodes.append(node_copy)
    return {
        "nodes": cleaned_nodes,
        "edges": graph_dict.get("edges", []),
    }


def canonical_json(model: BaseModel) -> str:
    """Serialize any schema model to deterministic, key-sorted UTF-8 JSON."""
    data = model.model_dump(mode="json")
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_definition_hash(
    definition: WorkflowDefinition,
    *,
    include_ui: bool = False,
) -> str:
    """Compute a deterministic SHA-256 content hash for a workflow definition."""
    data = definition.model_dump(mode="json")
    if not include_ui:
        data["graph"] = _strip_ui_from_graph(data["graph"])
    # Exclude optimistic concurrency revision from semantic graph hash
    data.pop("revision", None)
    payload = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def export_workflow_json_schema() -> dict[str, Any]:
    """Export the canonical JSON Schema for WorkflowDefinition (for frontend/OpenAPI/AI)."""
    return WorkflowDefinition.model_json_schema()


def export_graph_json_schema() -> dict[str, Any]:
    """Export the canonical JSON Schema for WorkflowGraph."""
    return WorkflowGraph.model_json_schema()
