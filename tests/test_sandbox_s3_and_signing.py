"""Tests for isolated code sandbox, S3/MinIO SigV4 artifact store, and Ed25519 package signing."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from flowpilot import (
    NodePackageManifest,
    NodeTestHarness,
    NodeTimeoutError,
    PackageTrustError,
    PackageTrustPolicy,
    SecurityPolicyError,
    generate_signing_keypair,
    sign_manifest,
    verify_manifest_signature,
)
from flowpilot_engine import ArtifactIntegrityError, S3ArtifactStore
from flowpilot_worker_code import (
    DockerCodeSandbox,
    PythonSandboxNode,
    SandboxLimits,
    SubprocessCodeSandbox,
    validate_user_code_ast,
)


@pytest.mark.asyncio
async def test_code_sandbox_executes_safe_code_and_blocks_escapes() -> None:
    # AST pre-flight blocks dangerous imports, calls, and dunder escapes
    with pytest.raises(SecurityPolicyError, match="Import 'os'"):
        validate_user_code_ast("import os\nresult = os.listdir('.')")

    with pytest.raises(SecurityPolicyError, match="Import from 'subprocess'"):
        validate_user_code_ast("from subprocess import Popen")

    with pytest.raises(SecurityPolicyError, match="dunder"):
        validate_user_code_ast("result = ().__class__.__mro__")

    with pytest.raises(SecurityPolicyError, match="open"):
        validate_user_code_ast("result = open('/etc/passwd').read()")

    # Safe deterministic code executes in an env-stripped isolated subprocess
    harness = NodeTestHarness()
    res = await harness.run(
        PythonSandboxNode(),
        {
            "code": (
                "import math\n"
                "print('computing')\n"
                "result = {'total': sum(input['nums']), 'root': math.sqrt(input['nums'][-1])}\n"
            ),
            "input_data": {"nums": [4, 9, 16]},
        },
    )
    assert res.result.output["result"] == {"total": 29, "root": 4.0}
    assert "computing" in res.result.output["stdout"]
    assert any(u.metric == "code_execution_ms" for u in res.usage_records)

    # Timeout enforcement kills runaway loops cleanly
    tight_sandbox = SubprocessCodeSandbox(limits=SandboxLimits(timeout_seconds=0.3))
    with pytest.raises(NodeTimeoutError):
        await tight_sandbox.execute("while True:\n    pass", {})

    # Docker sandbox builds hardened non-root, zero-egress, read-only container flags
    docker_cmd = DockerCodeSandbox(runtime="runsc").build_docker_command(Path("/tmp/fp_job"))
    assert "--network=none" in docker_cmd
    assert "--read-only" in docker_cmd
    assert "--cap-drop=ALL" in docker_cmd
    assert "--user=65534:65534" in docker_cmd
    assert "--runtime" in docker_cmd and "runsc" in docker_cmd
    assert "/var/run/docker.sock" not in " ".join(docker_cmd)


@pytest.mark.asyncio
async def test_s3_sigv4_artifact_store_roundtrip_and_integrity_check() -> None:
    objects: dict[str, bytes] = {}
    auth_headers: list[str] = []

    def _mock_s3(request: httpx.Request) -> httpx.Response:
        auth_headers.append(request.headers.get("authorization", ""))
        key = request.url.path
        if request.method == "PUT":
            objects[key] = request.content
            return httpx.Response(200)
        if request.method == "GET":
            if key not in objects:
                return httpx.Response(404)
            return httpx.Response(200, content=objects[key])
        return httpx.Response(405)

    store = S3ArtifactStore(
        endpoint_url="http://minio.local:9000",
        bucket="flowpilot-artifacts",
        access_key="minio-access",
        secret_key="minio-secret",
        transport=httpx.MockTransport(_mock_s3),
    )

    org_id = uuid4()
    run_id = uuid4()
    payload = {"items": [{"id": i, "val": f"item-{i}"} for i in range(10)]}

    ref = await store.put_json(organization_id=org_id, run_id=run_id, payload=payload)
    assert ref.uri.startswith(f"s3://flowpilot-artifacts/tenants/{org_id}/runs/{run_id}/artifacts/")
    assert auth_headers[-1].startswith("AWS4-HMAC-SHA256 Credential=minio-access/")

    loaded = await store.get_json(ref)
    assert loaded == payload

    # Tampering with stored object triggers SHA-256 ArtifactIntegrityError
    stored_key = next(iter(objects.keys()))
    objects[stored_key] = b'{"tampered":true}'
    with pytest.raises(ArtifactIntegrityError, match="SHA-256 mismatch"):
        await store.get_json(ref)


def test_ed25519_package_signing_verification_and_revocation() -> None:
    priv_b64, pub_b64 = generate_signing_keypair()
    _, other_pub_b64 = generate_signing_keypair()

    unsigned = NodePackageManifest(
        package_name="community-stripe-tools",
        package_version="1.2.0",
        first_party=False,
        entrypoints={"stripe.refund": "community_stripe:RefundNode"},
        permissions=("http.outbound",),
    )

    signed = sign_manifest(unsigned, priv_b64)
    assert signed.sha256_digest is not None
    assert signed.signature is not None
    assert verify_manifest_signature(signed, pub_b64) is True
    assert verify_manifest_signature(signed, other_pub_b64) is False

    # Tampering with manifest permissions after signing invalidates signature
    tampered = signed.model_copy(update={"permissions": ("http.outbound", "code.execute")})
    assert verify_manifest_signature(tampered, pub_b64) is False

    policy = PackageTrustPolicy(trusted_public_keys=[pub_b64])
    policy.assert_trusted(signed)

    with pytest.raises(PackageTrustError, match="signature verification"):
        policy.assert_trusted(unsigned)

    policy.revoke("community-stripe-tools", "1.2.0")
    with pytest.raises(PackageTrustError, match="revoked"):
        policy.assert_trusted(signed)
