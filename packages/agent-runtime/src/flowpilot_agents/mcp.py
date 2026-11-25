"""Native Model Context Protocol (MCP) JSON-RPC 2.0 outbound client bridge and inbound helpers."""

from __future__ import annotations

from typing import Any

import httpx

from flowpilot import ControlledHttpClient, NonRetryableNodeError
from flowpilot_agents.gateway import AgentToolGateway
from flowpilot_agents.models import ToolSpec
from flowpilot_schema import RiskLevel


class McpClientBridge:
    """Connects external MCP HTTP JSON-RPC servers into FlowPilot's governed AgentToolGateway."""

    def __init__(
        self,
        *,
        server_name: str,
        endpoint_url: str,
        allowed_domains: tuple[str, ...] = (),
        headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.server_name = server_name
        self._endpoint_url = endpoint_url
        self._headers = dict(headers or {})
        self._http = ControlledHttpClient(
            allowed_domains=allowed_domains,
            transport=transport,
        )
        self._req_id = 0

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._req_id += 1
        resp = await self._http.request(
            "POST",
            self._endpoint_url,
            headers={**self._headers, "Content-Type": "application/json"},
            json_body={
                "jsonrpc": "2.0",
                "id": self._req_id,
                "method": method,
                "params": params or {},
            },
        )
        if resp.status_code >= 400 or not isinstance(resp.body_json, dict):
            raise NonRetryableNodeError(
                f"MCP server '{self.server_name}' RPC '{method}' failed (HTTP {resp.status_code}).",
                code="MCP_RPC_ERROR",
            )
        if "error" in resp.body_json and resp.body_json["error"] is not None:
            raise NonRetryableNodeError(
                f"MCP server '{self.server_name}' returned error: {resp.body_json['error']}",
                code="MCP_SERVER_ERROR",
            )
        result = resp.body_json.get("result", {})
        return result if isinstance(result, dict) else {"value": result}

    async def list_remote_tools(self) -> list[dict[str, Any]]:
        res = await self._rpc("tools/list")
        tools = res.get("tools", [])
        return list(tools) if isinstance(tools, list) else []

    async def call_remote_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        return await self._rpc("tools/call", {"name": tool_name, "arguments": arguments})

    async def mount_into_gateway(
        self,
        gateway: AgentToolGateway,
        *,
        risk_level: RiskLevel = RiskLevel.MEDIUM,
        require_approval: bool = False,
        required_scopes: tuple[str, ...] = (),
        name_prefix: str | None = None,
    ) -> list[str]:
        """Discover remote MCP tools and register them into AgentToolGateway under policy."""
        prefix = name_prefix if name_prefix is not None else f"mcp.{self.server_name}."
        tools = await self.list_remote_tools()
        registered: list[str] = []

        for tool in tools:
            remote_name = str(tool.get("name", "")).strip()
            if not remote_name:
                continue
            gateway_name = f"{prefix}{remote_name}"
            spec = ToolSpec(
                name=gateway_name,
                description=str(tool.get("description", "")),
                risk_level=risk_level,
                require_approval=require_approval,
                required_scopes=required_scopes,
            )

            async def _invoke(
                args: dict[str, Any],
                *,
                _target: str = remote_name,
            ) -> Any:
                return await self.call_remote_tool(_target, args)

            gateway.register_tool(spec, _invoke)
            registered.append(gateway_name)

        return registered
