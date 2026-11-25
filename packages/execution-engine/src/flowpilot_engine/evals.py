"""Workflow & Agent Evaluation Suite for pre-publish and CI regression gates."""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from flowpilot_engine.runner import WorkflowExecutionEngine
from flowpilot_expressions import ExpressionError, evaluate_expression
from flowpilot_schema import RunStatus, WorkflowVersion


class EvalCase(BaseModel):
    """Single golden test case asserting status, output expressions, and cost/latency budgets."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=200)
    trigger_input: dict[str, Any] = Field(default_factory=dict)
    expected_status: RunStatus = RunStatus.SUCCEEDED
    assertions: list[str] = Field(
        default_factory=list,
        description=(
            "Restricted FlowPilot boolean expressions evaluated against "
            "{output, nodes, status, usage}."
        ),
    )
    max_duration_ms: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, ge=0.0)
    max_tokens: int | None = Field(default=None, ge=1)


class EvalCaseResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    passed: bool
    run_id: str
    actual_status: RunStatus
    duration_ms: int
    total_tokens: int
    total_cost_usd: float
    failures: list[str] = Field(default_factory=list)


class EvalSuiteResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_version_id: str
    passed: bool
    total_cases: int
    passed_cases: int
    pass_rate: float
    case_results: list[EvalCaseResult] = Field(default_factory=list)


class WorkflowEvalRunner:
    """Executes golden EvalCases against an immutable WorkflowVersion as a release gate."""

    def __init__(self, engine: WorkflowExecutionEngine) -> None:
        self._engine = engine

    async def run_suite(
        self,
        version: WorkflowVersion,
        cases: list[EvalCase],
    ) -> EvalSuiteResult:
        results: list[EvalCaseResult] = []

        for case in cases:
            t0 = time.monotonic()
            run = await self._engine.start_run(
                version,
                organization_id=version.organization_id,
                trigger_input=case.trigger_input,
                trigger_type="eval",
            )
            duration_ms = max(0, int((time.monotonic() - t0) * 1000))
            failures: list[str] = []

            if run.status != case.expected_status:
                failures.append(
                    f"Expected status {case.expected_status.value}, got {run.status.value}."
                )

            total_tokens = int(
                sum(
                    r.quantity
                    for r in run.usage_records
                    if r.metric in {"ai_tokens_input", "ai_tokens_output"}
                )
            )
            total_cost = sum(
                r.quantity for r in run.usage_records if r.metric == "ai_cost_usd"
            )

            if case.max_duration_ms is not None and duration_ms > case.max_duration_ms:
                failures.append(
                    f"Duration {duration_ms}ms exceeded max_duration_ms {case.max_duration_ms}ms."
                )
            if case.max_tokens is not None and total_tokens > case.max_tokens:
                failures.append(f"Tokens {total_tokens} exceeded max_tokens {case.max_tokens}.")
            if case.max_cost_usd is not None and total_cost > case.max_cost_usd:
                failures.append(
                    f"Cost ${total_cost:.4f} exceeded max_cost_usd ${case.max_cost_usd:.4f}."
                )

            run_output = await self._engine.payloads.unpack(run.output_ref)
            nodes_ctx: dict[str, Any] = {}
            for nid, nexec in run.node_executions.items():
                nodes_ctx[nid] = {
                    "status": nexec.status.value,
                    "output": await self._engine.payloads.unpack(nexec.output_ref),
                }

            eval_ctx = {
                "inputs": case.trigger_input,
                "trigger": case.trigger_input,
                "nodes": nodes_ctx,
                "run": {
                    "status": run.status.value,
                    "output": run_output,
                    "duration_ms": duration_ms,
                    "tokens": total_tokens,
                    "cost_usd": total_cost,
                },
            }
            for expr in case.assertions:
                try:
                    ok = bool(evaluate_expression(expr, eval_ctx))
                    if not ok:
                        failures.append(f"Assertion failed: {expr}")
                except ExpressionError as exc:
                    failures.append(f"Assertion error in '{expr}': {exc}")

            results.append(
                EvalCaseResult(
                    name=case.name,
                    passed=len(failures) == 0,
                    run_id=str(run.id),
                    actual_status=run.status,
                    duration_ms=duration_ms,
                    total_tokens=total_tokens,
                    total_cost_usd=total_cost,
                    failures=failures,
                )
            )

        passed_count = sum(1 for r in results if r.passed)
        total = len(results)
        return EvalSuiteResult(
            workflow_version_id=str(version.id),
            passed=passed_count == total,
            total_cases=total,
            passed_cases=passed_count,
            pass_rate=(passed_count / total) if total > 0 else 1.0,
            case_results=results,
        )
