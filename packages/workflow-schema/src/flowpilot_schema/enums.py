"""Core status, topology, policy, and event enumerations for FlowPilot."""

from enum import StrEnum


class RunStatus(StrEnum):
    """Canonical lifecycle states of a workflow run."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_RETRY = "WAITING_RETRY"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    TIMED_OUT = "TIMED_OUT"

    @property
    def is_terminal(self) -> bool:
        return self in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELED,
            RunStatus.TIMED_OUT,
        }


class NodeStatus(StrEnum):
    """Canonical lifecycle states of an individual node execution."""

    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    RETRYING = "RETRYING"
    CANCELED = "CANCELED"

    @property
    def is_terminal(self) -> bool:
        return self in {
            NodeStatus.SUCCEEDED,
            NodeStatus.FAILED,
            NodeStatus.SKIPPED,
            NodeStatus.CANCELED,
        }


class NodeKind(StrEnum):
    """High-level functional category of a workflow node."""

    TRIGGER = "trigger"
    ACTION = "action"
    CONTROL = "control"
    AI = "ai"
    AGENT = "agent"
    APPROVAL = "approval"
    CODE = "code"
    BROWSER = "browser"


class EdgeKind(StrEnum):
    """Semantic routing role of a directed graph edge."""

    DEFAULT = "default"
    TRUE = "true"
    FALSE = "false"
    ERROR = "error"


class WorkerPool(StrEnum):
    """Isolated execution pool responsible for running a node."""

    WORKFLOW = "workflow"
    INTEGRATIONS = "integrations"
    AGENTS = "agents"
    BROWSER = "browser"
    CODE = "code"


class SideEffectPolicy(StrEnum):
    """Declares whether a node mutates external state and whether retries are safe."""

    NONE = "none"
    IDEMPOTENT = "idempotent"
    IRREVERSIBLE = "irreversible"


class RiskLevel(StrEnum):
    """Risk classification for policy and approval enforcement."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ApprovalDecision(StrEnum):
    """Resolution state of a human-in-the-loop approval request."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class EventType(StrEnum):
    """Structured execution stream event types (SSE/audit compatible)."""

    RUN_CREATED = "run.created"
    RUN_STARTED = "run.started"
    RUN_WAITING_APPROVAL = "run.waiting_approval"
    RUN_WAITING_RETRY = "run.waiting_retry"
    RUN_RESUMED = "run.resumed"
    RUN_SUCCEEDED = "run.succeeded"
    RUN_FAILED = "run.failed"
    RUN_CANCELED = "run.canceled"
    RUN_TIMED_OUT = "run.timed_out"

    NODE_READY = "node.ready"
    NODE_STARTED = "node.started"
    NODE_RETRYING = "node.retrying"
    NODE_SUCCEEDED = "node.succeeded"
    NODE_FAILED = "node.failed"
    NODE_SKIPPED = "node.skipped"
    NODE_CANCELED = "node.canceled"

    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    USAGE_RECORDED = "usage.recorded"
    LOG = "log"
