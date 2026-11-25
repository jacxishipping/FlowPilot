"""12 extended first-party SaaS & utility connectors: Discord, Telegram, Gmail, Google Drive,
Jira, Airtable, Twilio, Shopify, AWS S3, OpenAI Embeddings, RSS Feed, and PagerDuty."""

from __future__ import annotations

import base64
import xml.etree.ElementTree as ET
from typing import Any, ClassVar

from pydantic import Field

from flowpilot import (
    BaseNode,
    CredentialError,
    CredentialRequirement,
    ExecutionContext,
    NodeConfigModel,
    NodeMetadata,
    NodeResult,
    NonRetryableNodeError,
    RetryableNodeError,
)
from flowpilot_schema import NodeKind, RiskLevel, SideEffectPolicy, WorkerPool


def _check(provider: str, status_code: int, body_text: str) -> None:
    if status_code == 429 or status_code >= 500:
        raise RetryableNodeError(f"{provider} transient HTTP {status_code}: {body_text[:200]}")
    if status_code >= 400:
        raise NonRetryableNodeError(f"{provider} API error HTTP {status_code}: {body_text[:200]}")


# 1. Discord Send Message
class DiscordSendMessageConfig(NodeConfigModel):
    webhook_url: str = Field(..., min_length=16, max_length=2048)
    content: str = Field(..., min_length=1, max_length=2000)
    username: str = "FlowPilot"


class DiscordSendMessageNode(BaseNode[DiscordSendMessageConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="discord.send_message",
        name="Discord Send Message",
        description="Posts a formatted message to a Discord channel via Webhook.",
        category="connectors",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.INTEGRATIONS,
        side_effect=SideEffectPolicy.IRREVERSIBLE,
        risk_level=RiskLevel.LOW,
        required_permissions=("discord.write",),
    )
    config_model: ClassVar[type[DiscordSendMessageConfig]] = DiscordSendMessageConfig

    async def execute(self, config: DiscordSendMessageConfig, ctx: ExecutionContext) -> NodeResult:
        resp = await ctx.http.request(
            "POST",
            config.webhook_url,
            json_body={"content": config.content, "username": config.username},
        )
        _check("discord", resp.status_code, resp.body_text)
        return NodeResult(output={"sent": True, "status_code": resp.status_code})


# 2. Telegram Send Message
class TelegramSendMessageConfig(NodeConfigModel):
    chat_id: str = Field(..., min_length=1, max_length=128)
    text: str = Field(..., min_length=1, max_length=4096)


class TelegramSendMessageNode(BaseNode[TelegramSendMessageConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="telegram.send_message",
        name="Telegram Send Message",
        description="Sends a message to a Telegram chat or channel via Bot API.",
        category="connectors",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.INTEGRATIONS,
        side_effect=SideEffectPolicy.IRREVERSIBLE,
        risk_level=RiskLevel.LOW,
        credentials=(
            CredentialRequirement(slot="telegram", credential_type="telegram_bot", required=True),
        ),
        required_permissions=("telegram.write",),
    )
    config_model: ClassVar[type[TelegramSendMessageConfig]] = TelegramSendMessageConfig

    async def execute(self, config: TelegramSendMessageConfig, ctx: ExecutionContext) -> NodeResult:
        cred = await ctx.get_credential("telegram")
        token = cred.get_secret("bot_token")
        resp = await ctx.http.request(
            "POST",
            f"https://api.telegram.org/bot{token}/sendMessage",
            json_body={"chat_id": config.chat_id, "text": config.text},
        )
        _check("telegram", resp.status_code, resp.body_text)
        data = resp.body_json if isinstance(resp.body_json, dict) else {}
        return NodeResult(
            output={
                "ok": data.get("ok", True),
                "message_id": data.get("result", {}).get("message_id"),
            }
        )


# 3. Gmail Send Email
class GmailSendEmailConfig(NodeConfigModel):
    to: str = Field(..., min_length=3, max_length=320)
    subject: str = Field(..., min_length=1, max_length=256)
    body: str = Field(..., min_length=1)


class GmailSendEmailNode(BaseNode[GmailSendEmailConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="gmail.send_email",
        name="Gmail Send Email",
        description="Sends an email through the Gmail REST API using OAuth credentials.",
        category="connectors",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.INTEGRATIONS,
        side_effect=SideEffectPolicy.IRREVERSIBLE,
        risk_level=RiskLevel.MEDIUM,
        credentials=(
            CredentialRequirement(slot="google", credential_type="google_oauth", required=True),
        ),
        required_permissions=("google.gmail.send",),
    )
    config_model: ClassVar[type[GmailSendEmailConfig]] = GmailSendEmailConfig

    async def execute(self, config: GmailSendEmailConfig, ctx: ExecutionContext) -> NodeResult:
        cred = await ctx.get_credential("google")
        token = cred.get_secret("token")
        mime = f"To: {config.to}\r\nSubject: {config.subject}\r\n\r\n{config.body}"
        raw_b64 = base64.urlsafe_b64encode(mime.encode("utf-8")).decode("ascii")
        resp = await ctx.http.request(
            "POST",
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={"Authorization": f"Bearer {token}"},
            json_body={"raw": raw_b64},
        )
        _check("gmail", resp.status_code, resp.body_text)
        data = resp.body_json if isinstance(resp.body_json, dict) else {}
        return NodeResult(output={"id": data.get("id"), "to": config.to})


# 4. Google Drive Upload File
class GoogleDriveUploadConfig(NodeConfigModel):
    filename: str = Field(..., min_length=1, max_length=256)
    mime_type: str = Field(default="text/plain")
    content: str = Field(..., min_length=1)
    folder_id: str | None = None


class GoogleDriveUploadNode(BaseNode[GoogleDriveUploadConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="google_drive.upload_file",
        name="Google Drive Upload",
        description="Uploads a file into Google Drive via Drive v3 API.",
        category="connectors",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.INTEGRATIONS,
        side_effect=SideEffectPolicy.IRREVERSIBLE,
        risk_level=RiskLevel.LOW,
        credentials=(
            CredentialRequirement(slot="google", credential_type="google_oauth", required=True),
        ),
        required_permissions=("google.drive.write",),
    )
    config_model: ClassVar[type[GoogleDriveUploadConfig]] = GoogleDriveUploadConfig

    async def execute(self, config: GoogleDriveUploadConfig, ctx: ExecutionContext) -> NodeResult:
        cred = await ctx.get_credential("google")
        token = cred.get_secret("token")
        meta: dict[str, Any] = {"name": config.filename, "mimeType": config.mime_type}
        if config.folder_id:
            meta["parents"] = [config.folder_id]
        resp = await ctx.http.request(
            "POST",
            "https://www.googleapis.com/drive/v3/files",
            headers={"Authorization": f"Bearer {token}"},
            json_body=meta,
        )
        _check("google_drive", resp.status_code, resp.body_text)
        data = resp.body_json if isinstance(resp.body_json, dict) else {}
        return NodeResult(output={"id": data.get("id"), "name": config.filename})


# 5. Jira Create Issue
class JiraCreateIssueConfig(NodeConfigModel):
    domain: str = Field(..., min_length=4, max_length=256)
    project_key: str = Field(..., min_length=1, max_length=32)
    summary: str = Field(..., min_length=1, max_length=256)
    issue_type: str = "Task"


class JiraCreateIssueNode(BaseNode[JiraCreateIssueConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="jira.create_issue",
        name="Jira Create Issue",
        description="Creates an issue in Atlassian Jira Cloud via REST API v3.",
        category="connectors",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.INTEGRATIONS,
        side_effect=SideEffectPolicy.IRREVERSIBLE,
        risk_level=RiskLevel.MEDIUM,
        credentials=(
            CredentialRequirement(slot="jira", credential_type="jira_api", required=True),
        ),
        required_permissions=("jira.write",),
    )
    config_model: ClassVar[type[JiraCreateIssueConfig]] = JiraCreateIssueConfig

    async def execute(self, config: JiraCreateIssueConfig, ctx: ExecutionContext) -> NodeResult:
        cred = await ctx.get_credential("jira")
        email = cred.get_secret("email")
        token = cred.get_secret("api_token")
        basic = base64.b64encode(f"{email}:{token}".encode()).decode("ascii")
        resp = await ctx.http.request(
            "POST",
            f"https://{config.domain}/rest/api/3/issue",
            headers={"Authorization": f"Basic {basic}"},
            json_body={
                "fields": {
                    "project": {"key": config.project_key},
                    "summary": config.summary,
                    "issuetype": {"name": config.issue_type},
                }
            },
        )
        _check("jira", resp.status_code, resp.body_text)
        data = resp.body_json if isinstance(resp.body_json, dict) else {}
        return NodeResult(output={"id": data.get("id"), "key": data.get("key")})


# 6. Airtable Create Record
class AirtableCreateRecordConfig(NodeConfigModel):
    base_id: str = Field(..., min_length=4, max_length=128)
    table_name: str = Field(..., min_length=1, max_length=128)
    fields: dict[str, Any] = Field(default_factory=dict)


class AirtableCreateRecordNode(BaseNode[AirtableCreateRecordConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="airtable.create_record",
        name="Airtable Create Record",
        description="Inserts a structured record into an Airtable base table.",
        category="connectors",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.INTEGRATIONS,
        side_effect=SideEffectPolicy.IRREVERSIBLE,
        risk_level=RiskLevel.LOW,
        credentials=(
            CredentialRequirement(slot="airtable", credential_type="airtable_api", required=True),
        ),
        required_permissions=("airtable.write",),
    )
    config_model: ClassVar[type[AirtableCreateRecordConfig]] = AirtableCreateRecordConfig

    async def execute(
        self,
        config: AirtableCreateRecordConfig,
        ctx: ExecutionContext,
    ) -> NodeResult:
        cred = await ctx.get_credential("airtable")
        token = cred.get_secret("token")
        resp = await ctx.http.request(
            "POST",
            f"https://api.airtable.com/v0/{config.base_id}/{config.table_name}",
            headers={"Authorization": f"Bearer {token}"},
            json_body={"fields": config.fields},
        )
        _check("airtable", resp.status_code, resp.body_text)
        data = resp.body_json if isinstance(resp.body_json, dict) else {}
        return NodeResult(output={"id": data.get("id"), "fields": data.get("fields", {})})


# 7. Twilio Send SMS
class TwilioSendSmsConfig(NodeConfigModel):
    account_sid: str = Field(..., min_length=6, max_length=64)
    from_number: str = Field(..., min_length=4, max_length=32)
    to_number: str = Field(..., min_length=4, max_length=32)
    body: str = Field(..., min_length=1, max_length=1600)


class TwilioSendSmsNode(BaseNode[TwilioSendSmsConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="twilio.send_sms",
        name="Twilio Send SMS",
        description="Dispatches an SMS message via the Twilio Messaging REST API.",
        category="connectors",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.INTEGRATIONS,
        side_effect=SideEffectPolicy.IRREVERSIBLE,
        risk_level=RiskLevel.HIGH,
        credentials=(
            CredentialRequirement(slot="twilio", credential_type="twilio_api", required=True),
        ),
        required_permissions=("twilio.sms",),
    )
    config_model: ClassVar[type[TwilioSendSmsConfig]] = TwilioSendSmsConfig

    async def execute(self, config: TwilioSendSmsConfig, ctx: ExecutionContext) -> NodeResult:
        cred = await ctx.get_credential("twilio")
        token = cred.get_secret("auth_token")
        basic = base64.b64encode(f"{config.account_sid}:{token}".encode()).decode("ascii")
        resp = await ctx.http.request(
            "POST",
            f"https://api.twilio.com/2010-04-01/Accounts/{config.account_sid}/Messages.json",
            headers={"Authorization": f"Basic {basic}"},
            json_body={"From": config.from_number, "To": config.to_number, "Body": config.body},
        )
        _check("twilio", resp.status_code, resp.body_text)
        data = resp.body_json if isinstance(resp.body_json, dict) else {}
        return NodeResult(output={"sid": data.get("sid"), "status": data.get("status")})


# 8. Shopify Get Order
class ShopifyGetOrderConfig(NodeConfigModel):
    shop_domain: str = Field(..., min_length=4, max_length=256)
    order_id: str = Field(..., min_length=1, max_length=64)


class ShopifyGetOrderNode(BaseNode[ShopifyGetOrderConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="shopify.get_order",
        name="Shopify Get Order",
        description="Fetches order details from Shopify Admin REST API.",
        category="connectors",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.INTEGRATIONS,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.LOW,
        credentials=(
            CredentialRequirement(slot="shopify", credential_type="shopify_api", required=True),
        ),
        required_permissions=("shopify.read",),
    )
    config_model: ClassVar[type[ShopifyGetOrderConfig]] = ShopifyGetOrderConfig

    async def execute(self, config: ShopifyGetOrderConfig, ctx: ExecutionContext) -> NodeResult:
        cred = await ctx.get_credential("shopify")
        token = cred.get_secret("access_token")
        resp = await ctx.http.request(
            "GET",
            f"https://{config.shop_domain}/admin/api/2024-10/orders/{config.order_id}.json",
            headers={"X-Shopify-Access-Token": token},
        )
        _check("shopify", resp.status_code, resp.body_text)
        data = resp.body_json if isinstance(resp.body_json, dict) else {}
        return NodeResult(output={"order": data.get("order", {})})


# 9. AWS S3 Put Object
class AwsS3PutObjectConfig(NodeConfigModel):
    endpoint_url: str = Field(..., min_length=8, max_length=2048)
    bucket: str = Field(..., min_length=1, max_length=128)
    key: str = Field(..., min_length=1, max_length=512)
    content: str = Field(...)
    content_type: str = "application/octet-stream"


class AwsS3PutObjectNode(BaseNode[AwsS3PutObjectConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="aws_s3.put_object",
        name="AWS S3 / R2 Put Object",
        description="Uploads text or JSON content to an S3-compatible bucket (AWS S3, R2, MinIO).",
        category="connectors",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.INTEGRATIONS,
        side_effect=SideEffectPolicy.IDEMPOTENT,
        risk_level=RiskLevel.MEDIUM,
        credentials=(
            CredentialRequirement(slot="s3", credential_type="http_auth", required=False),
        ),
        required_permissions=("s3.write",),
    )
    config_model: ClassVar[type[AwsS3PutObjectConfig]] = AwsS3PutObjectConfig

    async def execute(self, config: AwsS3PutObjectConfig, ctx: ExecutionContext) -> NodeResult:
        headers = {"Content-Type": config.content_type}
        try:
            cred = await ctx.get_credential("s3")
            if "token" in cred.secrets:
                headers["Authorization"] = f"Bearer {cred.get_secret('token')}"
        except CredentialError:
            pass
        url = f"{config.endpoint_url.rstrip('/')}/{config.bucket}/{config.key.lstrip('/')}"
        resp = await ctx.http.request("PUT", url, headers=headers, body=config.content)
        _check("aws_s3", resp.status_code, resp.body_text)
        return NodeResult(output={"bucket": config.bucket, "key": config.key, "stored": True})


# 10. OpenAI Embeddings
class OpenAiEmbeddingsConfig(NodeConfigModel):
    model: str = Field(default="text-embedding-3-small")
    input_text: str = Field(..., min_length=1)


class OpenAiEmbeddingsNode(BaseNode[OpenAiEmbeddingsConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="openai.embeddings",
        name="OpenAI Embeddings",
        description="Generates dense vector embeddings via OpenAI Embeddings API.",
        category="ai",
        kind=NodeKind.AI,
        worker_pool=WorkerPool.AGENTS,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.LOW,
        credentials=(
            CredentialRequirement(slot="openai", credential_type="openai_api", required=True),
        ),
        required_permissions=("ai.invoke",),
    )
    config_model: ClassVar[type[OpenAiEmbeddingsConfig]] = OpenAiEmbeddingsConfig

    async def execute(self, config: OpenAiEmbeddingsConfig, ctx: ExecutionContext) -> NodeResult:
        cred = await ctx.get_credential("openai")
        api_key = cred.get_secret("api_key")
        resp = await ctx.http.request(
            "POST",
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {api_key}"},
            json_body={"model": config.model, "input": config.input_text},
        )
        _check("openai", resp.status_code, resp.body_text)
        data = resp.body_json if isinstance(resp.body_json, dict) else {}
        emb = (data.get("data") or [{}])[0].get("embedding", [])
        return NodeResult(output={"embedding": emb, "dimensions": len(emb), "model": config.model})


# 11. RSS Feed Reader
class RssFeedReadConfig(NodeConfigModel):
    feed_url: str = Field(..., min_length=8, max_length=2048)
    limit: int = Field(default=10, ge=1, le=100)


class RssFeedReadNode(BaseNode[RssFeedReadConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="rss.read_feed",
        name="RSS Feed Reader",
        description="Fetches and parses RSS/Atom feed items with SSRF protection.",
        category="connectors",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.INTEGRATIONS,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.LOW,
        required_permissions=("http.outbound",),
    )
    config_model: ClassVar[type[RssFeedReadConfig]] = RssFeedReadConfig

    async def execute(self, config: RssFeedReadConfig, ctx: ExecutionContext) -> NodeResult:
        resp = await ctx.http.request("GET", config.feed_url)
        _check("rss", resp.status_code, resp.body_text)
        root = ET.fromstring(resp.body_text)
        items: list[dict[str, str]] = []
        for el in root.findall(".//item")[: config.limit]:
            items.append(
                {
                    "title": (el.findtext("title") or "").strip(),
                    "link": (el.findtext("link") or "").strip(),
                    "pub_date": (el.findtext("pubDate") or "").strip(),
                }
            )
        return NodeResult(output={"items": items, "count": len(items)})


# 12. PagerDuty Trigger Incident
class PagerDutyTriggerConfig(NodeConfigModel):
    summary: str = Field(..., min_length=1, max_length=1024)
    source: str = "flowpilot"
    severity: str = "error"


class PagerDutyTriggerIncidentNode(BaseNode[PagerDutyTriggerConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="pagerduty.trigger_incident",
        name="PagerDuty Trigger Incident",
        description="Triggers an on-call incident via PagerDuty Events API v2.",
        category="connectors",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.INTEGRATIONS,
        side_effect=SideEffectPolicy.IDEMPOTENT,
        risk_level=RiskLevel.HIGH,
        credentials=(
            CredentialRequirement(slot="pagerduty", credential_type="pagerduty_api", required=True),
        ),
        required_permissions=("pagerduty.write",),
    )
    config_model: ClassVar[type[PagerDutyTriggerConfig]] = PagerDutyTriggerConfig

    async def execute(self, config: PagerDutyTriggerConfig, ctx: ExecutionContext) -> NodeResult:
        cred = await ctx.get_credential("pagerduty")
        routing_key = cred.get_secret("routing_key")
        resp = await ctx.http.request(
            "POST",
            "https://events.pagerduty.com/v2/enqueue",
            json_body={
                "routing_key": routing_key,
                "event_action": "trigger",
                "dedup_key": ctx.idempotency_key or str(ctx.run_id),
                "payload": {
                    "summary": config.summary,
                    "source": config.source,
                    "severity": config.severity,
                },
            },
        )
        _check("pagerduty", resp.status_code, resp.body_text)
        data = resp.body_json if isinstance(resp.body_json, dict) else {}
        return NodeResult(
            output={"dedup_key": data.get("dedup_key"), "status": data.get("status", "success")}
        )


EXTENDED_CONNECTOR_NODES: tuple[type[BaseNode[Any, Any]], ...] = (
    DiscordSendMessageNode,
    TelegramSendMessageNode,
    GmailSendEmailNode,
    GoogleDriveUploadNode,
    JiraCreateIssueNode,
    AirtableCreateRecordNode,
    TwilioSendSmsNode,
    ShopifyGetOrderNode,
    AwsS3PutObjectNode,
    OpenAiEmbeddingsNode,
    RssFeedReadNode,
    PagerDutyTriggerIncidentNode,
)
