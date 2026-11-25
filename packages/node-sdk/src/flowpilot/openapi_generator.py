"""OpenAPI 3.x to FlowPilot Node auto-generator for instant SaaS & internal API connectors."""

from __future__ import annotations

import re
from typing import Any

from pydantic import Field, create_model

from flowpilot.context import ExecutionContext
from flowpilot.credentials import CredentialRequirement
from flowpilot.errors import CredentialError, NonRetryableNodeError, RetryableNodeError
from flowpilot.node import BaseNode, NodeMetadata
from flowpilot.schemas import NodeConfigModel, NodeResult
from flowpilot_schema import NodeKind, RiskLevel, SideEffectPolicy, WorkerPool

_ALLOWED_METHODS = ("get", "post", "put", "patch", "delete")


def _slugify(value: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
    return clean or "operation"


def generate_nodes_from_openapi(
    spec: dict[str, Any],
    *,
    prefix: str,
    base_url: str | None = None,
    credential_type: str = "http_auth",
    max_operations: int = 100,
) -> list[type[BaseNode[Any, Any]]]:
    """Synthesize typed, SSRF-safe FlowPilot BaseNode classes from an OpenAPI 3.x spec."""
    servers = spec.get("servers") or []
    resolved_base = (
        base_url
        or (str(servers[0].get("url", "")) if servers and isinstance(servers[0], dict) else "")
    ).rstrip("/")
    if not resolved_base:
        raise ValueError("OpenAPI spec must define servers[0].url or provide base_url.")

    clean_prefix = _slugify(prefix)
    generated: list[type[BaseNode[Any, Any]]] = []
    paths = spec.get("paths") or {}

    for path_template, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method in _ALLOWED_METHODS:
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue
            if len(generated) >= max_operations:
                return generated

            op_id = _slugify(str(op.get("operationId") or f"{method}_{path_template}"))
            node_type = f"{clean_prefix}.{op_id}"
            summary = str(op.get("summary") or f"{method.upper()} {path_template}")
            description = str(op.get("description") or summary)[:240]

            fields: dict[str, Any] = {
                "path_params": (dict[str, Any], Field(default_factory=dict)),
                "query_params": (dict[str, Any], Field(default_factory=dict)),
                "headers": (dict[str, str], Field(default_factory=dict)),
                "body": (Any, Field(default=None)),
            }
            cfg_cls = create_model(
                f"OpenApi_{clean_prefix}_{op_id}_Config",
                __base__=NodeConfigModel,
                **fields,
            )

            side_effect = (
                SideEffectPolicy.NONE
                if method == "get"
                else (
                    SideEffectPolicy.IDEMPOTENT
                    if method in ("put", "delete")
                    else SideEffectPolicy.IRREVERSIBLE
                )
            )
            meta = NodeMetadata(
                type=node_type,
                name=summary[:80],
                description=description,
                category="connectors",
                kind=NodeKind.ACTION,
                worker_pool=WorkerPool.INTEGRATIONS,
                side_effect=side_effect,
                risk_level=RiskLevel.LOW if method == "get" else RiskLevel.MEDIUM,
                credentials=(
                    CredentialRequirement(
                        slot="auth",
                        credential_type=credential_type,
                        required=False,
                    ),
                ),
                required_permissions=("http.outbound",),
            )

            def _make_node(
                _meta: NodeMetadata,
                _cfg: type[NodeConfigModel],
                _method: str,
                _path: str,
                _base: str,
                _op_id: str,
            ) -> type[BaseNode[Any, Any]]:
                class _GeneratedOpenApiNode(BaseNode[Any, Any]):
                    metadata = _meta
                    config_model = _cfg

                    async def execute(self, config: Any, ctx: ExecutionContext) -> NodeResult:
                        url_path = _path
                        for k, v in dict(config.path_params).items():
                            url_path = url_path.replace(f"{{{k}}}", str(v))
                        full_url = f"{_base}{url_path}"
                        hdrs = dict(config.headers)
                        try:
                            cred = await ctx.get_credential("auth")
                            if "token" in cred.secrets:
                                hdrs["Authorization"] = f"Bearer {cred.get_secret('token')}"
                            elif "api_key" in cred.secrets:
                                hdrs["X-API-Key"] = cred.get_secret("api_key")
                        except CredentialError:
                            pass
                        resp = await ctx.http.request(
                            _method.upper(),
                            full_url,
                            headers=hdrs,
                            params=dict(config.query_params) or None,
                            json_body=config.body if config.body is not None else None,
                        )
                        if resp.status_code == 429 or resp.status_code >= 500:
                            raise RetryableNodeError(f"OpenAPI transient HTTP {resp.status_code}")
                        if resp.status_code >= 400:
                            raise NonRetryableNodeError(f"OpenAPI error HTTP {resp.status_code}")
                        return NodeResult(
                            output={
                                "status_code": resp.status_code,
                                "body": resp.body_json
                                if resp.body_json is not None
                                else resp.body_text,
                            }
                        )

                _GeneratedOpenApiNode.__name__ = f"OpenApiNode_{clean_prefix}_{_op_id}"
                return _GeneratedOpenApiNode

            generated.append(
                _make_node(meta, cfg_cls, method, path_template, resolved_base, op_id)
            )

    return generated
