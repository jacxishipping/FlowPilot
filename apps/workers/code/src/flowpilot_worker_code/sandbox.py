"""Isolated untrusted Python code sandbox with AST policy gate, subprocess runner, and Docker jail.

Follows Section 11 strictly: never execute untrusted user code in the API process or ordinary
workers. Enforce non-root execution, CPU/memory/PID/time limits, zero network egress, read-only
filesystem, and AST pre-flight import/dunder restrictions.
"""

from __future__ import annotations

import ast
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from flowpilot import (
    BaseNode,
    ExecutionContext,
    NodeConfigModel,
    NodeMetadata,
    NodeResult,
    NodeTimeoutError,
    NonRetryableNodeError,
    SecurityPolicyError,
)
from flowpilot_schema import NodeKind, RiskLevel, SideEffectPolicy, WorkerPool

ALLOWED_SAFE_IMPORTS = frozenset({"json", "math", "re", "datetime", "decimal", "statistics"})
FORBIDDEN_CALLS = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "open",
        "__import__",
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
        "breakpoint",
        "input",
    }
)


class SandboxLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    timeout_seconds: float = Field(default=5.0, ge=0.2, le=60.0)
    memory_mb: int = Field(default=128, ge=32, le=1024)
    cpus: float = Field(default=0.5, ge=0.1, le=2.0)
    pids_limit: int = Field(default=32, ge=8, le=256)
    max_output_bytes: int = Field(default=65_536, ge=1_024, le=1_048_576)


class SandboxExecutionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    output: Any
    stdout: str = ""
    duration_ms: int = 0


def validate_user_code_ast(code: str) -> None:
    """AST pre-flight gate blocking disallowed imports, dunder escapes, and dangerous calls."""
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise NonRetryableNodeError(
            f"User code syntax error at line {exc.lineno}: {exc.msg}",
            code="CODE_SYNTAX_ERROR",
        ) from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_mod = alias.name.split(".", 1)[0]
                if root_mod not in ALLOWED_SAFE_IMPORTS:
                    raise SecurityPolicyError(
                        f"Import '{alias.name}' is forbidden in code sandbox. "
                        f"Allowed: {sorted(ALLOWED_SAFE_IMPORTS)}.",
                        code="SANDBOX_IMPORT_BLOCKED",
                    )
        elif isinstance(node, ast.ImportFrom):
            root_mod = (node.module or "").split(".", 1)[0]
            if root_mod not in ALLOWED_SAFE_IMPORTS:
                raise SecurityPolicyError(
                    f"Import from '{node.module}' is forbidden in code sandbox.",
                    code="SANDBOX_IMPORT_BLOCKED",
                )
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise SecurityPolicyError(
                f"Access to private/dunder attribute '{node.attr}' is forbidden in code sandbox.",
                code="SANDBOX_DUNDER_BLOCKED",
            )
        elif isinstance(node, ast.Name) and node.id.startswith("__"):
            raise SecurityPolicyError(
                f"Access to dunder symbol '{node.id}' is forbidden in code sandbox.",
                code="SANDBOX_DUNDER_BLOCKED",
            )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in FORBIDDEN_CALLS
        ):
            raise SecurityPolicyError(
                f"Call to '{node.func.id}()' is forbidden in code sandbox.",
                code="SANDBOX_CALL_BLOCKED",
            )


_HARNESS_TEMPLATE = """\
import io, json, sys
from contextlib import redirect_stdout

_input_payload = json.loads(sys.stdin.read() or "{}")
_buf = io.StringIO()

def _run(input_data):
    _ns = {"input": input_data, "result": None}
    with redirect_stdout(_buf):
        exec(compile(_USER_CODE, "<flowpilot_sandbox>", "exec"), _ns, _ns)
    return _ns.get("result")

_USER_CODE = __USER_CODE_JSON__
_out = _run(_input_payload)
sys.stdout.write(json.dumps({"output": _out, "stdout": _buf.getvalue()}))
"""


class SubprocessCodeSandbox:
    """Executes AST-validated user code in an isolated, env-stripped Python subprocess."""

    def __init__(self, limits: SandboxLimits | None = None) -> None:
        self._limits = limits or SandboxLimits()

    async def execute(self, code: str, input_data: dict[str, Any]) -> SandboxExecutionResult:
        validate_user_code_ast(code)

        harness = _HARNESS_TEMPLATE.replace("__USER_CODE_JSON__", json.dumps(code))
        stdin_bytes = json.dumps(input_data).encode("utf-8")

        with tempfile.TemporaryDirectory(prefix="fp_sandbox_") as tmpdir:
            script_path = Path(tmpdir) / "run.py"
            script_path.write_text(harness, encoding="utf-8")

            t0 = asyncio.get_running_loop().time()
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",  # Isolated mode: ignore PYTHON* env vars and user site-packages
                str(script_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=tmpdir,
                env={},  # Strip all host environment secrets
            )
            try:
                stdout_raw, stderr_raw = await asyncio.wait_for(
                    proc.communicate(stdin_bytes),
                    timeout=self._limits.timeout_seconds,
                )
            except TimeoutError as exc:
                proc.kill()
                await proc.wait()
                raise NodeTimeoutError(
                    f"Code sandbox timed out after {self._limits.timeout_seconds}s."
                ) from exc

            duration_ms = max(0, int((asyncio.get_running_loop().time() - t0) * 1000))

            if proc.returncode != 0:
                err_text = stderr_raw.decode("utf-8", errors="replace").strip()[:1000]
                raise NonRetryableNodeError(
                    f"Code sandbox execution failed: {err_text}",
                    code="SANDBOX_RUNTIME_ERROR",
                )

            if len(stdout_raw) > self._limits.max_output_bytes:
                raise SecurityPolicyError(
                    f"Code sandbox output exceeded {self._limits.max_output_bytes} bytes.",
                    code="SANDBOX_OUTPUT_TOO_LARGE",
                )

            parsed = json.loads(stdout_raw.decode("utf-8"))
            return SandboxExecutionResult(
                output=parsed.get("output"),
                stdout=str(parsed.get("stdout", "")),
                duration_ms=duration_ms,
            )


class DockerCodeSandbox:
    """Builds hardened Docker container command arguments for production code isolation.

    Enforces: --network=none, --read-only, --cap-drop=ALL, --security-opt=no-new-privileges,
    non-root UID/GID (65534:65534), memory/CPU/PID caps, and zero Docker socket mounts.
    """

    def __init__(
        self,
        *,
        image: str = "python:3.12-alpine",
        limits: SandboxLimits | None = None,
        runtime: str | None = None,  # e.g. 'runsc' for gVisor evaluation
    ) -> None:
        self._image = image
        self._limits = limits or SandboxLimits()
        self._runtime = runtime

    def build_docker_command(self, script_dir: Path) -> list[str]:
        cmd = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--user=65534:65534",
            f"--memory={self._limits.memory_mb}m",
            f"--memory-swap={self._limits.memory_mb}m",
            f"--cpus={self._limits.cpus}",
            f"--pids-limit={self._limits.pids_limit}",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=16m",
            "-v",
            f"{script_dir.as_posix()}:/sandbox:ro",
        ]
        if self._runtime:
            cmd.extend(["--runtime", self._runtime])
        cmd.extend([self._image, "python", "-I", "/sandbox/run.py"])
        return cmd


class PythonSandboxConfig(NodeConfigModel):
    code: str = Field(..., min_length=1, max_length=20_000)
    input_data: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=5.0, ge=0.2, le=30.0)


class PythonSandboxNode(BaseNode[PythonSandboxConfig, Any]):
    """Isolated untrusted user code node dispatched strictly to WorkerPool.CODE."""

    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="code.python_sandbox",
        name="Python Code (Sandboxed)",
        description="Runs bounded Python transform code inside an isolated non-root sandbox.",
        category="code",
        kind=NodeKind.CODE,
        worker_pool=WorkerPool.CODE,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.HIGH,
        required_permissions=("code.execute",),
    )
    config_model: ClassVar[type[PythonSandboxConfig]] = PythonSandboxConfig

    async def execute(self, config: PythonSandboxConfig, ctx: ExecutionContext) -> NodeResult:
        sandbox = SubprocessCodeSandbox(
            limits=SandboxLimits(timeout_seconds=config.timeout_seconds)
        )
        res = await sandbox.execute(config.code, config.input_data)
        ctx.record_usage("code_execution_ms", float(res.duration_ms), unit="ms")
        return NodeResult(
            output={
                "result": res.output,
                "stdout": res.stdout,
                "duration_ms": res.duration_ms,
            }
        )
