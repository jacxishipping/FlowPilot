"""Comprehensive tests for full n8n-parity features:
Logic/Data/GraphQL/JS Code nodes, Ollama + built-in agent tools, 12 extended connectors,
OpenAPI auto-connector, Single-Node Test Step, Folders, Data Tables, Binary Files,
Knowledge RAG, Insights Analytics, Hosted ChatHub, and Interactive AI Assistant."""

from __future__ import annotations

import base64
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from flowpilot import InMemoryCredentialResolver, NodeTestHarness
from flowpilot_agents import (
    AgentDefinition,
    AgentToolGateway,
    OllamaModelProvider,
    register_builtin_agent_tools,
)
from flowpilot_api import Settings, create_app
from flowpilot_nodes_connectors import (
    AirtableCreateRecordNode,
    AwsS3PutObjectNode,
    DiscordSendMessageNode,
    GmailSendEmailNode,
    GoogleDriveUploadNode,
    JiraCreateIssueNode,
    OpenAiEmbeddingsNode,
    PagerDutyTriggerIncidentNode,
    RssFeedReadNode,
    ShopifyGetOrderNode,
    TelegramSendMessageNode,
    TwilioSendSmsNode,
)
from flowpilot_nodes_core import (
    AggregateNode,
    FilterNode,
    GraphQLRequestNode,
    JavaScriptCodeNode,
    MergeNode,
    SortNode,
    SplitInBatchesNode,
    SwitchNode,
)


@pytest.mark.asyncio
async def test_core_logic_data_graphql_and_js_sandbox_nodes() -> None:
    def _gql_mock(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"viewer": {"login": "alice"}}})

    harness = NodeTestHarness(http_transport=httpx.MockTransport(_gql_mock))

    sw = await harness.run(
        SwitchNode(),
        config={"value": "vip", "routes": {"fast_lane": "vip", "normal": "free"}},
    )
    assert sw.result.branch == "fast_lane"

    mg = await harness.run(MergeNode(), config={"mode": "append", "inputs": [[1, 2], [3]]})
    assert mg.result.output["items"] == [1, 2, 3]

    sp = await harness.run(
        SplitInBatchesNode(), config={"items": [1, 2, 3, 4, 5], "batch_size": 2}
    )
    assert sp.result.output["batch_count"] == 3

    flt = await harness.run(
        FilterNode(),
        config={
            "items": [{"name": "a", "score": 90}, {"name": "b", "score": 40}],
            "field": "score",
            "operator": "gte",
            "value": 80,
        },
    )
    assert flt.result.output["matched_count"] == 1
    assert flt.result.output["items"][0]["name"] == "a"

    srt = await harness.run(
        SortNode(),
        config={
            "items": [{"n": 3}, {"n": 1}, {"n": 2}],
            "field": "n",
            "descending": True,
        },
    )
    assert [x["n"] for x in srt.result.output["items"]] == [3, 2, 1]

    agg = await harness.run(
        AggregateNode(),
        config={
            "items": [{"team": "a", "amt": 10}, {"team": "a", "amt": 20}, {"team": "b", "amt": 5}],
            "field": "amt",
            "group_by": "team",
            "operation": "sum",
        },
    )
    assert agg.result.output["groups"] == {"a": 30.0, "b": 5.0}

    gql = await harness.run(
        GraphQLRequestNode(),
        config={"endpoint": "https://api.github.com/graphql", "query": "{ viewer { login } }"},
    )
    assert gql.result.output["data"]["viewer"]["login"] == "alice"

    js = await harness.run(
        JavaScriptCodeNode(),
        config={
            "code": "console.log('hi'); return { doubled: input.x * 2 };",
            "input_data": {"x": 21},
        },
    )
    assert js.result.output["result"] == {"doubled": 42}
    assert js.result.output["logs"] == ["hi"]


@pytest.mark.asyncio
async def test_ollama_provider_builtin_tools_and_twelve_extended_connectors() -> None:
    def _ollama_mock(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/embeddings"):
            return httpx.Response(200, json={"embedding": [0.1, 0.2, 0.3]})
        return httpx.Response(
            200,
            json={
                "message": {"content": "Local Llama answer"},
                "prompt_eval_count": 12,
                "eval_count": 8,
                "done_reason": "stop",
            },
        )

    ollama = OllamaModelProvider(
        base_url="http://ollama.local:11434",
        transport=httpx.MockTransport(_ollama_mock),
    )
    comp = await ollama.complete(
        provider="ollama",
        model="llama3.2",
        messages=[{"role": "user", "content": "Hello"}],
    )
    assert comp.content == "Local Llama answer"
    assert comp.cost_usd == 0.0
    assert await ollama.embed(model="nomic-embed-text", text="hello") == [0.1, 0.2, 0.3]

    # Built-in agent tools (web_search & http_fetch)
    def _tools_mock(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.duckduckgo.com":
            return httpx.Response(
                200,
                json={"Heading": "FlowPilot", "AbstractText": "Deterministic workflow engine"},
            )
        return httpx.Response(200, text="fetched page content")

    gw = AgentToolGateway()
    mounted = register_builtin_agent_tools(gw, http_transport=httpx.MockTransport(_tools_mock))
    assert "builtin.web_search" in mounted
    assert "builtin.http_fetch" in mounted
    agent = AgentDefinition(
        name="researcher",
        objective="search",
        allowed_tools=["builtin.web_search", "builtin.http_fetch"],
    )
    search_out = await gw.invoke(
        agent=agent,
        organization_id=uuid4(),
        tool_name="builtin.web_search",
        arguments={"query": "FlowPilot"},
        call_counts={},
    )
    assert search_out["heading"] == "FlowPilot"

    # 12 extended SaaS connectors
    def _saas_mock(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "discord.com":
            return httpx.Response(204)
        if host == "api.telegram.org":
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})
        if host == "gmail.googleapis.com":
            return httpx.Response(200, json={"id": "msg_1"})
        if host == "www.googleapis.com":
            return httpx.Response(200, json={"id": "drv_1"})
        if host.endswith(".atlassian.net"):
            return httpx.Response(201, json={"id": "100", "key": "OPS-7"})
        if host == "api.airtable.com":
            return httpx.Response(200, json={"id": "rec_1", "fields": {"Name": "Acme"}})
        if host == "api.twilio.com":
            return httpx.Response(201, json={"sid": "SM123", "status": "queued"})
        if host.endswith(".myshopify.com"):
            return httpx.Response(200, json={"order": {"id": 555, "total_price": "99.00"}})
        if host == "s3.example.com":
            return httpx.Response(200)
        if host == "api.openai.com":
            return httpx.Response(200, json={"data": [{"embedding": [0.5, 0.5]}]})
        if host == "news.example.com":
            xml = "<rss><channel><item><title>Launch</title><link>https://x</link></item></channel></rss>"
            return httpx.Response(200, text=xml)
        if host == "events.pagerduty.com":
            return httpx.Response(202, json={"dedup_key": "pd-1", "status": "success"})
        return httpx.Response(404)

    org_id = uuid4()
    res = InMemoryCredentialResolver()
    c_tg = res.add(
        organization_id=org_id, credential_type="telegram_bot", secrets={"bot_token": "b"}
    )
    c_gg = res.add(organization_id=org_id, credential_type="google_oauth", secrets={"token": "g"})
    c_jr = res.add(
        organization_id=org_id,
        credential_type="jira_api",
        secrets={"email": "a@b.com", "api_token": "t"},
    )
    c_at = res.add(organization_id=org_id, credential_type="airtable_api", secrets={"token": "a"})
    c_tw = res.add(
        organization_id=org_id, credential_type="twilio_api", secrets={"auth_token": "t"}
    )
    c_sh = res.add(
        organization_id=org_id, credential_type="shopify_api", secrets={"access_token": "s"}
    )
    c_oa = res.add(organization_id=org_id, credential_type="openai_api", secrets={"api_key": "k"})
    c_pd = res.add(
        organization_id=org_id, credential_type="pagerduty_api", secrets={"routing_key": "r"}
    )
    ch = NodeTestHarness(
        organization_id=org_id,
        credential_resolver=res,
        http_transport=httpx.MockTransport(_saas_mock),
    )

    assert (
        await ch.run(
            DiscordSendMessageNode(),
            config={"webhook_url": "https://discord.com/api/webhooks/1/a", "content": "hi"},
        )
    ).result.output["sent"] is True
    assert (
        await ch.run(
            TelegramSendMessageNode(),
            config={"chat_id": "123", "text": "hi"},
            credential_bindings={"telegram": c_tg},
        )
    ).result.output["message_id"] == 99
    assert (
        await ch.run(
            GmailSendEmailNode(),
            config={"to": "u@acme.io", "subject": "Hi", "body": "Body"},
            credential_bindings={"google": c_gg},
        )
    ).result.output["id"] == "msg_1"
    assert (
        await ch.run(
            GoogleDriveUploadNode(),
            config={"filename": "report.txt", "content": "ok"},
            credential_bindings={"google": c_gg},
        )
    ).result.output["id"] == "drv_1"
    assert (
        await ch.run(
            JiraCreateIssueNode(),
            config={"domain": "acme.atlassian.net", "project_key": "OPS", "summary": "Fix"},
            credential_bindings={"jira": c_jr},
        )
    ).result.output["key"] == "OPS-7"
    assert (
        await ch.run(
            AirtableCreateRecordNode(),
            config={"base_id": "app123", "table_name": "Leads", "fields": {"Name": "Acme"}},
            credential_bindings={"airtable": c_at},
        )
    ).result.output["id"] == "rec_1"
    assert (
        await ch.run(
            TwilioSendSmsNode(),
            config={
                "account_sid": "AC123456",
                "from_number": "+15551112222",
                "to_number": "+15553334444",
                "body": "Alert",
            },
            credential_bindings={"twilio": c_tw},
        )
    ).result.output["sid"] == "SM123"
    assert (
        await ch.run(
            ShopifyGetOrderNode(),
            config={"shop_domain": "acme.myshopify.com", "order_id": "555"},
            credential_bindings={"shopify": c_sh},
        )
    ).result.output["order"]["id"] == 555
    assert (
        await ch.run(
            AwsS3PutObjectNode(),
            config={
                "endpoint_url": "https://s3.example.com",
                "bucket": "bkt",
                "key": "a.txt",
                "content": "hello",
            },
        )
    ).result.output["stored"] is True
    assert (
        await ch.run(
            OpenAiEmbeddingsNode(),
            config={"input_text": "hello world"},
            credential_bindings={"openai": c_oa},
        )
    ).result.output["dimensions"] == 2
    assert (
        await ch.run(
            RssFeedReadNode(),
            config={"feed_url": "https://news.example.com/rss.xml"},
        )
    ).result.output["count"] == 1
    assert (
        await ch.run(
            PagerDutyTriggerIncidentNode(),
            config={"summary": "API down"},
            credential_bindings={"pagerduty": c_pd},
        )
    ).result.output["status"] == "success"


@pytest.mark.asyncio
async def test_parity_apis_test_step_tables_files_rag_insights_chathub_assistant_openapi(
    tmp_path: Path,
) -> None:
    db_file = tmp_path / "parity.db"

    def _openapi_mock(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"customer_id": "C-42", "tier": "enterprise"})

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{db_file.as_posix()}",
        auto_create_tables=True,
        master_key="parity-test-key",
        session_secret="parity-session-secret",
    )
    app = create_app(settings, http_transport=httpx.MockTransport(_openapi_mock))

    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        boot = (
            await client.post(
                "/api/v1/auth/bootstrap",
                json={
                    "email": "builder@parity.io",
                    "organization_name": "Parity Labs",
                    "organization_slug": "parity-labs",
                },
            )
        ).json()
        headers = {"Authorization": f"Bearer {boot['api_key']}"}
        proj_id = boot["project_id"]

        # 1. Single-Node Execution ("Test Step")
        step_res = (
            await client.post(
                "/api/v1/nodes/test",
                headers=headers,
                json={
                    "node_id": "calc",
                    "node_type": "core.set_data",
                    "config": {"values": {"total": "{{ nodes.prev.output.price * 2 }}"}},
                    "pinned_nodes": {"prev": {"price": 75}},
                },
            )
        ).json()
        assert step_res["ok"] is True
        assert step_res["output"] == {"total": 150}

        # 2. Folders CRUD
        folder = (
            await client.post(
                "/api/v1/folders",
                headers=headers,
                json={"project_id": proj_id, "name": "Finance Ops"},
            )
        ).json()
        assert len((await client.get("/api/v1/folders", headers=headers)).json()) == 1
        assert (
            await client.delete(f"/api/v1/folders/{folder['id']}", headers=headers)
        ).status_code == 204

        # 3. Built-In Data Tables CRUD
        tbl = (
            await client.post(
                "/api/v1/tables",
                headers=headers,
                json={"name": "orders", "description": "Order ledger"},
            )
        ).json()
        row = (
            await client.post(
                f"/api/v1/tables/{tbl['id']}/rows",
                headers=headers,
                json={"data": {"order_id": "ORD-1", "amount": 250}},
            )
        ).json()
        rows = (await client.get(f"/api/v1/tables/{tbl['id']}/rows", headers=headers)).json()
        assert len(rows) == 1
        assert rows[0]["data"]["order_id"] == "ORD-1"
        assert (
            await client.delete(f"/api/v1/tables/{tbl['id']}/rows/{row['id']}", headers=headers)
        ).status_code == 204

        # 4. Binary File Upload & Download
        raw_bytes = b"FlowPilot PDF binary content"
        up = (
            await client.post(
                "/api/v1/files/upload",
                headers=headers,
                json={
                    "filename": "invoice.pdf",
                    "mime_type": "application/pdf",
                    "content_base64": base64.b64encode(raw_bytes).decode("ascii"),
                },
            )
        ).json()
        dl = await client.get(f"/api/v1/files/{up['id']}/download", headers=headers)
        assert dl.status_code == 200
        assert dl.content == raw_bytes

        # 5. Knowledge Base / RAG Ingestion & Semantic Search
        ing = (
            await client.post(
                "/api/v1/knowledge/documents",
                headers=headers,
                json={
                    "title": "Refund Policy",
                    "content": "Customers receive full refunds within 30 days of purchase.",
                },
            )
        ).json()
        assert ing["chunk_count"] == 1
        rag = (
            await client.post(
                "/api/v1/knowledge/search",
                headers=headers,
                json={"query": "30 days refund", "top_k": 3},
            )
        ).json()
        assert rag["count"] == 1
        assert "30 days" in rag["chunks"][0]["content"]

        # 6. Hosted ChatHub backed by ChatTrigger + KnowledgeSearch + DataTable workflow
        chat_wf = (
            await client.post(
                "/api/v1/workflows",
                headers=headers,
                json={
                    "project_id": proj_id,
                    "name": "RAG Support Chat",
                    "graph": {
                        "nodes": [
                            {"id": "chat", "type": "core.chat_trigger"},
                            {
                                "id": "kb",
                                "type": "core.knowledge_search",
                                "config": {"query": "{{ nodes.chat.output.message }}", "top_k": 2},
                            },
                            {
                                "id": "log_row",
                                "type": "core.data_table",
                                "config": {
                                    "table_name": "chat_audit",
                                    "operation": "insert",
                                    "data": {"q": "{{ nodes.chat.output.message }}"},
                                },
                            },
                            {
                                "id": "reply",
                                "type": "core.set_data",
                                "config": {"values": {"reply": "{{ nodes.kb.output.context }}"}},
                            },
                        ],
                        "edges": [
                            {"id": "e1", "source": "chat", "target": "kb"},
                            {"id": "e2", "source": "kb", "target": "log_row"},
                            {"id": "e3", "source": "log_row", "target": "reply"},
                        ],
                    },
                },
            )
        ).json()
        await client.post(
            f"/api/v1/workflows/{chat_wf['id']}/publish",
            headers=headers,
            json={"changelog": "chat v1", "activate": True},
        )
        chat_res = (
            await client.post(
                f"/api/v1/chat/{chat_wf['id']}/messages",
                headers=headers,
                json={"message": "What is the refund window?"},
            )
        ).json()
        assert chat_res["status"] == "SUCCEEDED"
        assert "30 days" in chat_res["reply"]

        sess_history = (
            await client.get(
                f"/api/v1/chat/{chat_wf['id']}/sessions/{chat_res['session_id']}",
                headers=headers,
            )
        ).json()
        assert len(sess_history["messages"]) == 2

        # 7. Insights Dashboard
        insights = (await client.get("/api/v1/insights", headers=headers)).json()
        assert insights["total_runs"] >= 1
        assert insights["succeeded_runs"] >= 1
        assert insights["estimated_hours_saved"] > 0

        # 8. Interactive AI Assistant (edits live graph)
        assist = (
            await client.post(
                "/api/v1/ai/assistant",
                headers=headers,
                json={
                    "message": "Add a Switch router and a Slack alert",
                    "workflow_name": "Ops Flow",
                    "graph": {
                        "nodes": [{"id": "t", "type": "core.manual_trigger"}],
                        "edges": [],
                    },
                },
            )
        ).json()
        assert assist["modified"] is True
        node_types = {n["type"] for n in assist["graph"]["nodes"]}
        assert "core.switch" in node_types
        assert "slack.post_message" in node_types

        # 9. Dynamic OpenAPI-to-Node Import & Single-Node Execution of Imported Node
        imp = (
            await client.post(
                "/api/v1/connectors/openapi/import",
                headers=headers,
                json={
                    "prefix": "acme_crm",
                    "spec": {
                        "openapi": "3.0.0",
                        "servers": [{"url": "https://crm.acme.io"}],
                        "paths": {
                            "/v1/customers/{id}": {
                                "get": {
                                    "operationId": "get_customer",
                                    "summary": "Get CRM Customer",
                                }
                            }
                        },
                    },
                },
            )
        ).json()
        assert imp["registered_count"] == 1
        assert imp["node_types"] == ["acme_crm.get_customer"]

        tested_gen = (
            await client.post(
                "/api/v1/nodes/test",
                headers=headers,
                json={
                    "node_id": "crm_call",
                    "node_type": "acme_crm.get_customer",
                    "config": {"path_params": {"id": "C-42"}},
                },
            )
        ).json()
        assert tested_gen["ok"] is True
        assert tested_gen["output"]["body"]["customer_id"] == "C-42"
