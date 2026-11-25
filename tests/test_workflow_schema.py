"""Tests for flowpilot_schema canonical graph models, versioning, and JSON Schema export."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from flowpilot_schema import (
    EdgeDefinition,
    NodeDefinition,
    NodeStatus,
    NodeUI,
    Position,
    RunStatus,
    WorkflowDefinition,
    WorkflowGraph,
    WorkflowVersion,
    canonical_json,
    compute_definition_hash,
    export_graph_json_schema,
    export_workflow_json_schema,
)


def test_canonical_hash_is_invariant_to_ui_layout_and_revision() -> None:
    org_id = uuid4()
    wf_id = uuid4()
    proj_id = uuid4()

    wf_a = WorkflowDefinition(
        id=wf_id,
        organization_id=org_id,
        project_id=proj_id,
        name="Order Intake",
        revision=1,
        graph=WorkflowGraph(
            nodes=[
                NodeDefinition(
                    id="trigger",
                    type="core.manual_trigger",
                    ui=NodeUI(position=Position(x=10, y=20)),
                ),
                NodeDefinition(
                    id="set_fields",
                    type="core.set_data",
                    config={"values": {"b": 2, "a": 1}},
                    ui=NodeUI(position=Position(x=200, y=20)),
                ),
            ],
            edges=[EdgeDefinition(id="e1", source="trigger", target="set_fields")],
        ),
    )

    wf_b = wf_a.model_copy(deep=True)
    wf_b.revision = 5
    wf_b.graph.nodes[0].ui = NodeUI(position=Position(x=999, y=500), collapsed=True)
    wf_b.graph.nodes[1].config = {"values": {"a": 1, "b": 2}}

    assert compute_definition_hash(wf_a) == compute_definition_hash(wf_b)
    assert compute_definition_hash(wf_a, include_ui=True) != compute_definition_hash(
        wf_b, include_ui=True
    )


def test_workflow_version_is_immutable_snapshot() -> None:
    wf = WorkflowDefinition(
        name="Snapshot Test",
        graph=WorkflowGraph(nodes=[NodeDefinition(id="start", type="core.manual_trigger")]),
    )
    version = WorkflowVersion.from_definition(wf, version_number=1, changelog="Initial release")

    # Mutating original draft must not affect the pinned WorkflowVersion
    wf.name = "Mutated Draft Name"
    assert version.definition.name == "Snapshot Test"
    assert version.version_number == 1
    assert len(version.content_hash) == 64

    with pytest.raises(ValidationError):
        version.version_number = 2  # type: ignore[misc]


def test_node_id_validation_and_json_schema_export() -> None:
    with pytest.raises(ValidationError):
        NodeDefinition(id="123_starts_with_digit", type="core.set_data")

    with pytest.raises(ValidationError):
        NodeDefinition(id="has space", type="core.set_data")

    wf_schema = export_workflow_json_schema()
    graph_schema = export_graph_json_schema()
    assert wf_schema["title"] == "WorkflowDefinition"
    assert "graph" in wf_schema["properties"]
    assert "$defs" in graph_schema and "WorkflowGraph" in graph_schema["$defs"]

    wf = WorkflowDefinition(
        name="Canonical JSON",
        graph=WorkflowGraph(nodes=[NodeDefinition(id="start", type="core.manual_trigger")]),
    )
    serialized = canonical_json(wf)
    assert '"schema_version":"1.0.0"' in serialized


def test_terminal_status_properties() -> None:
    assert RunStatus.SUCCEEDED.is_terminal
    assert RunStatus.FAILED.is_terminal
    assert RunStatus.CANCELED.is_terminal
    assert RunStatus.TIMED_OUT.is_terminal
    assert not RunStatus.RUNNING.is_terminal
    assert not RunStatus.WAITING_APPROVAL.is_terminal

    assert NodeStatus.SUCCEEDED.is_terminal
    assert NodeStatus.SKIPPED.is_terminal
    assert not NodeStatus.RETRYING.is_terminal
    assert not NodeStatus.PENDING.is_terminal
