"""S3/MinIO-compatible ArtifactStore using lightweight AWS Signature V4 signing over httpx."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx

from flowpilot_schema import ArtifactReference


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sigv4_headers(
    *,
    method: str,
    url: str,
    payload: bytes,
    access_key: str,
    secret_key: str,
    region: str,
    content_type: str | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    """Compute AWS Signature Version 4 Authorization headers for S3/MinIO."""
    dt = (now or datetime.now(UTC)).astimezone(UTC)
    amz_date = dt.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = dt.strftime("%Y%m%d")
    parsed = urlparse(url)
    host = parsed.netloc
    canonical_uri = parsed.path or "/"
    payload_hash = hashlib.sha256(payload).hexdigest()

    headers_to_sign: dict[str, str] = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if content_type:
        headers_to_sign["content-type"] = content_type

    sorted_keys = sorted(headers_to_sign.keys())
    canonical_headers = "".join(f"{k}:{headers_to_sign[k]}\n" for k in sorted_keys)
    signed_headers = ";".join(sorted_keys)

    canonical_request = "\n".join(
        [method.upper(), canonical_uri, "", canonical_headers, signed_headers, payload_hash]
    )
    credential_scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    k_date = _sign(f"AWS4{secret_key}".encode(), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, "s3")
    k_signing = _sign(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    result = {
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }
    if content_type:
        result["Content-Type"] = content_type
    return result


class ArtifactIntegrityError(RuntimeError):
    """Raised when an object fetched from S3/MinIO fails SHA-256 integrity verification."""


class S3ArtifactStore:
    """Tenant-scoped S3/MinIO object store keeping large payloads outside Temporal history."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoint = endpoint_url.rstrip("/")
        self._bucket = bucket
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region
        self._transport = transport

    def _object_url(self, key: str) -> str:
        return f"{self._endpoint}/{self._bucket}/{key.lstrip('/')}"

    async def put_json(
        self,
        *,
        organization_id: UUID,
        run_id: UUID,
        payload: Any,
    ) -> ArtifactReference:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
        sha = hashlib.sha256(raw).hexdigest()
        artifact_id = uuid4()
        key = f"tenants/{organization_id}/runs/{run_id}/artifacts/{artifact_id}.json"
        url = self._object_url(key)
        headers = _sigv4_headers(
            method="PUT",
            url=url,
            payload=raw,
            access_key=self._access_key,
            secret_key=self._secret_key,
            region=self._region,
            content_type="application/json",
        )

        async with httpx.AsyncClient(transport=self._transport) as client:
            resp = await client.put(url, content=raw, headers=headers)
            resp.raise_for_status()

        return ArtifactReference(
            artifact_id=artifact_id,
            uri=f"s3://{self._bucket}/{key}",
            content_type="application/json",
            size_bytes=len(raw),
            sha256=sha,
        )

    async def get_json(self, ref: ArtifactReference) -> Any:
        prefix = f"s3://{self._bucket}/"
        if not ref.uri.startswith(prefix):
            raise ArtifactIntegrityError(
                f"Artifact URI '{ref.uri}' does not match configured bucket '{self._bucket}'."
            )
        key = ref.uri[len(prefix) :]
        url = self._object_url(key)
        headers = _sigv4_headers(
            method="GET",
            url=url,
            payload=b"",
            access_key=self._access_key,
            secret_key=self._secret_key,
            region=self._region,
        )

        async with httpx.AsyncClient(transport=self._transport) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            raw = resp.content

        actual_sha = hashlib.sha256(raw).hexdigest()
        if actual_sha != ref.sha256:
            raise ArtifactIntegrityError(
                f"SHA-256 mismatch for artifact '{ref.artifact_id}': "
                f"expected {ref.sha256}, got {actual_sha}."
            )
        return json.loads(raw.decode("utf-8"))
