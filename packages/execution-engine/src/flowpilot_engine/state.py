"""Explicit state machine transitions for WorkflowRun and NodeExecution."""

from __future__ import annotations

from flowpilot_schema import NodeStatus, RunStatus

ALLOWED_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.CANCELED, RunStatus.FAILED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_APPROVAL,
            RunStatus.WAITING_RETRY,
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELED,
            RunStatus.TIMED_OUT,
        }
    ),
    RunStatus.WAITING_APPROVAL: frozenset(
        {RunStatus.RUNNING, RunStatus.CANCELED, RunStatus.TIMED_OUT, RunStatus.FAILED}
    ),
    RunStatus.WAITING_RETRY: frozenset(
        {RunStatus.RUNNING, RunStatus.CANCELED, RunStatus.TIMED_OUT, RunStatus.FAILED}
    ),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELED: frozenset(),
    RunStatus.TIMED_OUT: frozenset(),
}

ALLOWED_NODE_TRANSITIONS: dict[NodeStatus, frozenset[NodeStatus]] = {
    NodeStatus.PENDING: frozenset({NodeStatus.READY, NodeStatus.SKIPPED, NodeStatus.CANCELED}),
    NodeStatus.READY: frozenset({NodeStatus.RUNNING, NodeStatus.SKIPPED, NodeStatus.CANCELED}),
    NodeStatus.RUNNING: frozenset(
        {
            NodeStatus.SUCCEEDED,
            NodeStatus.FAILED,
            NodeStatus.RETRYING,
            NodeStatus.CANCELED,
            NodeStatus.READY,  # Re-queued after human approval pause
        }
    ),
    NodeStatus.RETRYING: frozenset({NodeStatus.RUNNING, NodeStatus.FAILED, NodeStatus.CANCELED}),
    NodeStatus.SUCCEEDED: frozenset(),
    NodeStatus.FAILED: frozenset(),
    NodeStatus.SKIPPED: frozenset(),
    NodeStatus.CANCELED: frozenset(),
}


class InvalidStateTransitionError(RuntimeError):
    """Raised when attempting an illegal RunStatus or NodeStatus transition."""


def transition_run_status(current: RunStatus, target: RunStatus) -> RunStatus:
    if current == target:
        return target
    if target not in ALLOWED_RUN_TRANSITIONS[current]:
        raise InvalidStateTransitionError(
            f"Illegal RunStatus transition: {current.value} -> {target.value}"
        )
    return target


def transition_node_status(current: NodeStatus, target: NodeStatus) -> NodeStatus:
    if current == target:
        return target
    if target not in ALLOWED_NODE_TRANSITIONS[current]:
        raise InvalidStateTransitionError(
            f"Illegal NodeStatus transition: {current.value} -> {target.value}"
        )
    return target
