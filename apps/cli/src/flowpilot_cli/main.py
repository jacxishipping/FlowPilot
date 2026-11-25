"""Flow-as-Code GitOps CLI supporting validate, test, diff, pull, and publish."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx

from flowpilot_api.app import build_full_node_registry
from flowpilot_engine import WorkflowExecutionEngine
from flowpilot_schema import WorkflowDefinition, WorkflowVersion
from flowpilot_validator import WorkflowValidator


def compute_graph_diff(old_def: WorkflowDefinition, new_def: WorkflowDefinition) -> dict[str, Any]:
    """Compute structured node/edge diff between two canonical WorkflowDefinitions."""
    old_nodes = {n.id: n for n in old_def.graph.nodes}
    new_nodes = {n.id: n for n in new_def.graph.nodes}
    old_edges = {e.id: e for e in old_def.graph.edges}
    new_edges = {e.id: e for e in new_def.graph.edges}

    added_nodes = sorted(set(new_nodes) - set(old_nodes))
    removed_nodes = sorted(set(old_nodes) - set(new_nodes))
    modified_nodes: list[dict[str, Any]] = []
    for nid in sorted(set(old_nodes) & set(new_nodes)):
        a = old_nodes[nid].model_dump(mode="json", exclude={"ui"})
        b = new_nodes[nid].model_dump(mode="json", exclude={"ui"})
        if a != b:
            changed_fields = sorted(k for k in (set(a) | set(b)) if a.get(k) != b.get(k))
            modified_nodes.append({"node_id": nid, "changed_fields": changed_fields})

    return {
        "added_nodes": added_nodes,
        "removed_nodes": removed_nodes,
        "modified_nodes": modified_nodes,
        "added_edges": sorted(set(new_edges) - set(old_edges)),
        "removed_edges": sorted(set(old_edges) - set(new_edges)),
    }


def _load_definition(path: Path) -> WorkflowDefinition:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "definition" in raw:
        raw = raw["definition"]
    return WorkflowDefinition.model_validate(raw)


def cmd_validate(file_path: Path) -> int:
    definition = _load_definition(file_path)
    validator = WorkflowValidator(build_full_node_registry())
    result = validator.validate(definition)
    print(json.dumps(result.model_dump(mode="json"), indent=2))
    return 0 if result.is_valid else 1


def cmd_test(file_path: Path, trigger_input_json: str) -> int:
    definition = _load_definition(file_path)
    trigger_input = json.loads(trigger_input_json)
    version = WorkflowVersion.from_definition(definition, version_number=1)
    engine = WorkflowExecutionEngine(registry=build_full_node_registry())
    run = asyncio.run(engine.start_run(version, trigger_input=trigger_input))
    print(json.dumps(run.model_dump(mode="json"), indent=2))
    return 0 if run.status.value in {"SUCCEEDED", "WAITING_APPROVAL"} else 1


def cmd_diff(old_path: Path, new_path: Path) -> int:
    diff = compute_graph_diff(_load_definition(old_path), _load_definition(new_path))
    print(json.dumps(diff, indent=2))
    return 0


def cmd_pull(workflow_id: str, api_url: str, api_key: str, out_path: Path) -> int:
    resp = httpx.get(
        f"{api_url.rstrip('/')}/api/v1/workflows/{workflow_id}",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=20.0,
    )
    resp.raise_for_status()
    out_path.write_text(json.dumps(resp.json()["definition"], indent=2), encoding="utf-8")
    print(f"Pulled workflow {workflow_id} -> {out_path}")
    return 0


def cmd_publish(
    workflow_id: str,
    file_path: Path,
    api_url: str,
    api_key: str,
    changelog: str,
) -> int:
    definition = _load_definition(file_path)
    headers = {"Authorization": f"Bearer {api_key}"}
    base = api_url.rstrip("/")
    current = httpx.get(f"{base}/api/v1/workflows/{workflow_id}", headers=headers, timeout=20.0)
    current.raise_for_status()
    revision = int(current.json()["revision"])

    patched = httpx.patch(
        f"{base}/api/v1/workflows/{workflow_id}",
        headers=headers,
        json={
            "expected_revision": revision,
            "name": definition.name,
            "description": definition.description,
            "graph": definition.graph.model_dump(mode="json"),
        },
        timeout=20.0,
    )
    patched.raise_for_status()

    pub = httpx.post(
        f"{base}/api/v1/workflows/{workflow_id}/publish",
        headers=headers,
        json={"changelog": changelog, "activate": True},
        timeout=20.0,
    )
    pub.raise_for_status()
    print(json.dumps(pub.json(), indent=2))
    return 0


def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flowpilot", description="FlowPilot Flow-as-Code CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_val = sub.add_parser("validate", help="Validate a canonical workflow JSON file")
    p_val.add_argument("file", type=Path)

    p_test = sub.add_parser("test", help="Execute a canonical workflow JSON file locally")
    p_test.add_argument("file", type=Path)
    p_test.add_argument("--input", default="{}", dest="trigger_input")

    p_diff = sub.add_parser("diff", help="Diff two canonical workflow JSON files")
    p_diff.add_argument("old_file", type=Path)
    p_diff.add_argument("new_file", type=Path)

    p_pull = sub.add_parser("pull", help="Pull workflow JSON from FlowPilot API")
    p_pull.add_argument("workflow_id")
    p_pull.add_argument("--api-url", default="http://localhost:8000")
    p_pull.add_argument("--api-key", required=True)
    p_pull.add_argument("--out", type=Path, required=True)

    p_pub = sub.add_parser("publish", help="Upload and publish a workflow JSON file")
    p_pub.add_argument("workflow_id")
    p_pub.add_argument("file", type=Path)
    p_pub.add_argument("--api-url", default="http://localhost:8000")
    p_pub.add_argument("--api-key", required=True)
    p_pub.add_argument("--changelog", default="Published via flowpilot CLI")

    args = parser.parse_args(argv)
    if args.command == "validate":
        return cmd_validate(args.file)
    if args.command == "test":
        return cmd_test(args.file, args.trigger_input)
    if args.command == "diff":
        return cmd_diff(args.old_file, args.new_file)
    if args.command == "pull":
        return cmd_pull(args.workflow_id, args.api_url, args.api_key, args.out)
    if args.command == "publish":
        return cmd_publish(args.workflow_id, args.file, args.api_url, args.api_key, args.changelog)
    return 1


def main() -> None:
    sys.exit(run_cli())
