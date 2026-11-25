"""Built-in governed tools for FlowPilot Agents: web_search, http_fetch, and knowledge_search."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from flowpilot import ControlledHttpClient
from flowpilot_agents.gateway import AgentToolGateway
from flowpilot_agents.models import ToolSpec
from flowpilot_schema import RiskLevel

KnowledgeLookupFn = Callable[[str, int], Awaitable[list[dict[str, Any]]]]


def register_builtin_agent_tools(
    gateway: AgentToolGateway,
    *,
    http_transport: httpx.AsyncBaseTransport | None = None,
    knowledge_lookup: KnowledgeLookupFn | None = None,
) -> list[str]:
    """Mount built-in SSRF-safe web search, HTTP fetch, and RAG search tools into a ToolGateway."""
    http = ControlledHttpClient(transport=http_transport)
    registered: list[str] = []

    async def _web_search(args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query", "")).strip()
        if not query:
            return {"results": [], "abstract": ""}
        resp = await http.request(
            "GET",
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
        )
        data = resp.body_json if isinstance(resp.body_json, dict) else {}
        topics = [
            {"text": t.get("Text", ""), "url": t.get("FirstURL", "")}
            for t in (data.get("RelatedTopics") or [])[:5]
            if isinstance(t, dict) and t.get("Text")
        ]
        return {
            "query": query,
            "heading": data.get("Heading", ""),
            "abstract": data.get("AbstractText", ""),
            "results": topics,
        }

    gateway.register_tool(
        ToolSpec(
            name="builtin.web_search",
            description="Searches the public web via DuckDuckGo Instant Answer API.",
            risk_level=RiskLevel.LOW,
            max_calls_per_run=10,
        ),
        _web_search,
    )
    registered.append("builtin.web_search")

    async def _http_fetch(args: dict[str, Any]) -> dict[str, Any]:
        url = str(args.get("url", "")).strip()
        resp = await http.request("GET", url)
        return {
            "status_code": resp.status_code,
            "text": resp.body_text[:4000],
        }

    gateway.register_tool(
        ToolSpec(
            name="builtin.http_fetch",
            description="Fetches a public web page or JSON endpoint with SSRF protection.",
            risk_level=RiskLevel.MEDIUM,
            max_calls_per_run=10,
        ),
        _http_fetch,
    )
    registered.append("builtin.http_fetch")

    if knowledge_lookup is not None:

        async def _kb_search(args: dict[str, Any]) -> dict[str, Any]:
            query = str(args.get("query", "")).strip()
            top_k = max(1, min(20, int(args.get("top_k", 5))))
            chunks = await knowledge_lookup(query, top_k)
            return {"query": query, "chunks": chunks, "count": len(chunks)}

        gateway.register_tool(
            ToolSpec(
                name="builtin.knowledge_search",
                description="Retrieves grounding context chunks from the workspace Knowledge Base.",
                risk_level=RiskLevel.LOW,
                max_calls_per_run=15,
            ),
            _kb_search,
        )
        registered.append("builtin.knowledge_search")

    return registered
