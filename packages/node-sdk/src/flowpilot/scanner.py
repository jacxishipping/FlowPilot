"""Static AST security scanner and submission verification pipeline for marketplace packages."""

from __future__ import annotations

import ast

from pydantic import BaseModel, ConfigDict, Field

from flowpilot.manifest import NodePackageManifest
from flowpilot.signing import PackageTrustError, PackageTrustPolicy, sign_manifest

_DANGEROUS_IMPORTS = frozenset(
    {"subprocess", "ctypes", "pty", "shutil", "socket", "urllib", "requests", "pickle"}
)
_DANGEROUS_CALLS = frozenset({"eval", "exec", "compile", "__import__", "system", "popen"})


class ScanFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str
    line: int
    code: str
    severity: str  # 'critical' | 'high' | 'warning'
    message: str


class PackageScanReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    findings: list[ScanFinding] = Field(default_factory=list)


def scan_package_source(files: dict[str, str]) -> PackageScanReport:
    """Scan third-party Python node package sources for unsafe OS, exec, or egress primitives."""
    findings: list[ScanFinding] = []

    for filename, source in files.items():
        try:
            tree = ast.parse(source, filename=filename)
        except SyntaxError as exc:
            findings.append(
                ScanFinding(
                    file=filename,
                    line=exc.lineno or 1,
                    code="SYNTAX_ERROR",
                    severity="critical",
                    message=f"Syntax error: {exc.msg}",
                )
            )
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    if root in _DANGEROUS_IMPORTS:
                        findings.append(
                            ScanFinding(
                                file=filename,
                                line=node.lineno,
                                code="DISALLOWED_IMPORT",
                                severity="critical",
                                message=(
                                    f"Import '{alias.name}' is disallowed in third-party nodes; "
                                    "use ExecutionContext.http instead."
                                ),
                            )
                        )
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".", 1)[0]
                if root in _DANGEROUS_IMPORTS:
                    findings.append(
                        ScanFinding(
                            file=filename,
                            line=node.lineno,
                            code="DISALLOWED_IMPORT",
                            severity="critical",
                            message=f"Import from '{node.module}' is disallowed.",
                        )
                    )
            elif (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
                and node.attr in {"system", "popen", "environ", "remove", "unlink"}
            ):
                findings.append(
                    ScanFinding(
                        file=filename,
                        line=node.lineno,
                        code="OS_ACCESS_BLOCKED",
                        severity="critical",
                        message=f"Direct os.{node.attr} access is forbidden in third-party nodes.",
                    )
                )
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in _DANGEROUS_CALLS
            ):
                findings.append(
                    ScanFinding(
                        file=filename,
                        line=node.lineno,
                        code="DYNAMIC_EXECUTION_BLOCKED",
                        severity="critical",
                        message=f"Call to '{node.func.id}()' is forbidden in third-party nodes.",
                    )
                )

    has_blocking = any(f.severity in {"critical", "high"} for f in findings)
    return PackageScanReport(passed=not has_blocking, findings=findings)


class MarketplaceSubmissionPipeline:
    """End-to-end review gate: static security scan -> Ed25519 signing -> trust verification."""

    def __init__(
        self,
        *,
        trust_policy: PackageTrustPolicy,
        signing_private_key_b64: str | None = None,
    ) -> None:
        self._policy = trust_policy
        self._signing_key = signing_private_key_b64

    def review_and_approve(
        self,
        *,
        manifest: NodePackageManifest,
        source_files: dict[str, str],
    ) -> tuple[NodePackageManifest, PackageScanReport]:
        report = scan_package_source(source_files)
        if not report.passed:
            raise PackageTrustError(
                f"Package '{manifest.package_name}' failed security scan "
                f"({len(report.findings)} finding(s))."
            )
        approved_manifest = manifest
        if self._signing_key is not None and not manifest.signature:
            approved_manifest = sign_manifest(manifest, self._signing_key)
        self._policy.assert_trusted(approved_manifest)
        return approved_manifest, report
