"""Deterministic DAG graph interpreter for ready-node scheduling, skips, and error branches."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from flowpilot_schema import (
    EdgeDefinition,
    EdgeKind,
    NodeDefinition,
    NodeExecution,
    NodeStatus,
    WorkflowGraph,
)


@dataclass(frozen=True)
class StepPlan:
    """Deterministic scheduling decision computed by GraphInterpreter."""

    ready_node_ids: list[str]
    skipped_node_ids: list[str]
    unhandled_failed_node_ids: list[str]
    is_complete: bool


class GraphInterpreter:
    """Pure, deterministic interpreter over canonical WorkflowGraph definitions."""

    def __init__(self, graph: WorkflowGraph) -> None:
        self._graph = graph
        self._nodes_by_id: dict[str, NodeDefinition] = {n.id: n for n in graph.nodes}
        self._incoming: dict[str, list[EdgeDefinition]] = defaultdict(list)
        self._outgoing: dict[str, list[EdgeDefinition]] = defaultdict(list)
        for edge in graph.edges:
            self._incoming[edge.target].append(edge)
            self._outgoing[edge.source].append(edge)

    @property
    def nodes_by_id(self) -> dict[str, NodeDefinition]:
        return self._nodes_by_id

    def incoming_edges(self, node_id: str) -> list[EdgeDefinition]:
        return list(self._incoming.get(node_id, ()))

    def outgoing_edges(self, node_id: str) -> list[EdgeDefinition]:
        return list(self._outgoing.get(node_id, ()))

    def has_error_branch(self, node_id: str) -> bool:
        return any(edge.kind == EdgeKind.ERROR for edge in self._outgoing.get(node_id, ()))

    def is_edge_active(self, edge: EdgeDefinition, source_exec: NodeExecution) -> bool:
        """Determine whether a resolved edge activates its target node."""
        if edge.kind == EdgeKind.ERROR:
            return source_exec.status == NodeStatus.FAILED

        if source_exec.status != NodeStatus.SUCCEEDED:
            return False

        if edge.kind == EdgeKind.DEFAULT:
            return True

        if edge.kind == EdgeKind.TRUE:
            return source_exec.branch_taken == "true"

        if edge.kind == EdgeKind.FALSE:
            return source_exec.branch_taken == "false"

        return False

    def compute_step(self, executions: dict[str, NodeExecution]) -> StepPlan:
        """Compute newly skipped nodes, ready nodes, unhandled failures, and completion state."""
        # Simulate skip propagation until fixed point so cascading skips resolve in one pass
        effective_status: dict[str, NodeStatus] = {
            nid: exec_state.status for nid, exec_state in executions.items()
        }
        newly_skipped: list[str] = []

        # Disabled nodes become SKIPPED immediately
        for nid, node in self._nodes_by_id.items():
            if node.disabled and effective_status[nid] == NodeStatus.PENDING:
                effective_status[nid] = NodeStatus.SKIPPED
                newly_skipped.append(nid)

        changed = True
        while changed:
            changed = False
            for node in self._graph.nodes:
                nid = node.id
                if effective_status[nid] != NodeStatus.PENDING:
                    continue
                in_edges = self._incoming.get(nid, [])
                if not in_edges:
                    continue
                # Check if all upstream sources are terminal
                if not all(effective_status[e.source].is_terminal for e in in_edges):
                    continue
                # All incoming edges resolved: activate if ANY incoming edge is active
                any_active = False
                for edge in in_edges:
                    src_exec = executions[edge.source]
                    if effective_status[edge.source] == NodeStatus.SKIPPED:
                        continue
                    if self.is_edge_active(edge, src_exec):
                        any_active = True
                        break
                if not any_active:
                    effective_status[nid] = NodeStatus.SKIPPED
                    newly_skipped.append(nid)
                    changed = True

        # Find nodes eligible to become READY (in deterministic graph definition order)
        ready_node_ids: list[str] = []
        for node in self._graph.nodes:
            nid = node.id
            status = effective_status[nid]
            if status == NodeStatus.READY:
                ready_node_ids.append(nid)
                continue
            if status != NodeStatus.PENDING:
                continue

            in_edges = self._incoming.get(nid, [])
            if not in_edges:
                ready_node_ids.append(nid)
                continue

            if not all(effective_status[e.source].is_terminal for e in in_edges):
                continue

            if any(
                effective_status[e.source] != NodeStatus.SKIPPED
                and self.is_edge_active(e, executions[e.source])
                for e in in_edges
            ):
                ready_node_ids.append(nid)

        # Detect any FAILED node that lacks an ERROR edge handler
        unhandled_failed = [
            node.id
            for node in self._graph.nodes
            if effective_status[node.id] == NodeStatus.FAILED and not self.has_error_branch(node.id)
        ]

        all_terminal = all(status.is_terminal for status in effective_status.values())
        return StepPlan(
            ready_node_ids=ready_node_ids,
            skipped_node_ids=newly_skipped,
            unhandled_failed_node_ids=unhandled_failed,
            is_complete=all_terminal,
        )
