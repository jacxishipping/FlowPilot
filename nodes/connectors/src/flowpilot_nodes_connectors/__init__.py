"""First-party SaaS connectors: GitHub, Linear, Notion, Google Sheets, Stripe, HubSpot."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from flowpilot import (
    BaseNode,
    CredentialRequirement,
    ExecutionContext,
    NodeConfigModel,
    NodeMetadata,
    NodePackageManifest,
    NodeRegistry,
    NodeResult,
    NonRetryableNodeError,
    RetryableNodeError,
)
from flowpilot_schema import NodeKind, RiskLevel, SideEffectPolicy, WorkerPool


def _check_http(provider: str, status_code: int, body_text: str) -> None:
    if status_code == 429 or status_code >= 500:
        raise RetryableNodeError(
            f"{provider} API transient HTTP {status_code}: {body_text[:250]}",
            code=f"{provider.upper()}_HTTP_{status_code}",
        )
    if status_code >= 400:
        raise NonRetryableNodeError(
            f"{provider} API error HTTP {status_code}: {body_text[:250]}",
            code=f"{provider.upper()}_HTTP_{status_code}",
        )


# 1. GitHub Create Issue
class GitHubCreateIssueConfig(NodeConfigModel):
    owner: str = Field(..., min_length=1, max_length=128)
    repo: str = Field(..., min_length=1, max_length=128)
    title: str = Field(..., min_length=1, max_length=256)
    body: str = ""
    labels: list[str] = Field(default_factory=list)


class GitHubCreateIssueNode(BaseNode[GitHubCreateIssueConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="github.create_issue",
        name="GitHub Create Issue",
        description="Creates an issue in a GitHub repository via authenticated REST API.",
        category="connectors",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.INTEGRATIONS,
        side_effect=SideEffectPolicy.IRREVERSIBLE,
        risk_level=RiskLevel.MEDIUM,
        credentials=(
            CredentialRequirement(
                slot="github",
                credential_type="github_oauth",
                required=True,
                scopes=("repo",),
            ),
        ),
        required_permissions=("github.write",),
    )
    config_model: ClassVar[type[GitHubCreateIssueConfig]] = GitHubCreateIssueConfig

    async def execute(self, config: GitHubCreateIssueConfig, ctx: ExecutionContext) -> NodeResult:
        cred = await ctx.get_credential("github")
        token = cred.get_secret("token")
        url = f"https://api.github.com/repos/{config.owner}/{config.repo}/issues"
        resp = await ctx.http.request(
            "POST",
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            json_body={"title": config.title, "body": config.body, "labels": config.labels},
        )
        _check_http("github", resp.status_code, resp.body_text)
        data = resp.body_json if isinstance(resp.body_json, dict) else {}
        return NodeResult(
            output={
                "id": data.get("id"),
                "number": data.get("number"),
                "url": data.get("html_url"),
            }
        )


# 2. Linear Create Issue
class LinearCreateIssueConfig(NodeConfigModel):
    team_id: str = Field(..., min_length=1, max_length=128)
    title: str = Field(..., min_length=1, max_length=256)
    description: str = ""
    priority: int = Field(default=2, ge=0, le=4)


class LinearCreateIssueNode(BaseNode[LinearCreateIssueConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="linear.create_issue",
        name="Linear Create Issue",
        description="Creates a tracked issue in Linear via GraphQL API.",
        category="connectors",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.INTEGRATIONS,
        side_effect=SideEffectPolicy.IRREVERSIBLE,
        risk_level=RiskLevel.MEDIUM,
        credentials=(
            CredentialRequirement(slot="linear", credential_type="linear_api", required=True),
        ),
        required_permissions=("linear.write",),
    )
    config_model: ClassVar[type[LinearCreateIssueConfig]] = LinearCreateIssueConfig

    async def execute(self, config: LinearCreateIssueConfig, ctx: ExecutionContext) -> NodeResult:
        cred = await ctx.get_credential("linear")
        api_key = cred.get_secret("api_key")
        query = (
            "mutation IssueCreate($input: IssueCreateInput!) { "
            "issueCreate(input: $input) { success issue { id identifier url } } }"
        )
        resp = await ctx.http.request(
            "POST",
            "https://api.linear.app/graphql",
            headers={"Authorization": api_key},
            json_body={
                "query": query,
                "variables": {
                    "input": {
                        "teamId": config.team_id,
                        "title": config.title,
                        "description": config.description,
                        "priority": config.priority,
                    }
                },
            },
        )
        _check_http("linear", resp.status_code, resp.body_text)
        data = resp.body_json if isinstance(resp.body_json, dict) else {}
        issue = data.get("data", {}).get("issueCreate", {}).get("issue", {})
        return NodeResult(
            output={
                "id": issue.get("id"),
                "identifier": issue.get("identifier"),
                "url": issue.get("url"),
            }
        )


# 3. Notion Create Page
class NotionCreatePageConfig(NodeConfigModel):
    database_id: str = Field(..., min_length=1, max_length=128)
    title: str = Field(..., min_length=1, max_length=256)
    properties: dict[str, Any] = Field(default_factory=dict)


class NotionCreatePageNode(BaseNode[NotionCreatePageConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="notion.create_page",
        name="Notion Create Page",
        description="Inserts a new page into a Notion database.",
        category="connectors",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.INTEGRATIONS,
        side_effect=SideEffectPolicy.IRREVERSIBLE,
        risk_level=RiskLevel.LOW,
        credentials=(
            CredentialRequirement(slot="notion", credential_type="notion_api", required=True),
        ),
        required_permissions=("notion.write",),
    )
    config_model: ClassVar[type[NotionCreatePageConfig]] = NotionCreatePageConfig

    async def execute(self, config: NotionCreatePageConfig, ctx: ExecutionContext) -> NodeResult:
        cred = await ctx.get_credential("notion")
        token = cred.get_secret("token")
        props = {
            "Name": {"title": [{"text": {"content": config.title}}]},
            **config.properties,
        }
        resp = await ctx.http.request(
            "POST",
            "https://api.notion.com/v1/pages",
            headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"},
            json_body={"parent": {"database_id": config.database_id}, "properties": props},
        )
        _check_http("notion", resp.status_code, resp.body_text)
        data = resp.body_json if isinstance(resp.body_json, dict) else {}
        return NodeResult(output={"id": data.get("id"), "url": data.get("url")})


# 4. Google Sheets Append Row
class GoogleSheetsAppendRowConfig(NodeConfigModel):
    spreadsheet_id: str = Field(..., min_length=1, max_length=256)
    range_name: str = Field(default="Sheet1!A1")
    values: list[Any] = Field(..., min_length=1)


class GoogleSheetsAppendRowNode(BaseNode[GoogleSheetsAppendRowConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="google_sheets.append_row",
        name="Google Sheets Append Row",
        description="Appends a structured row to a Google Sheets spreadsheet.",
        category="connectors",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.INTEGRATIONS,
        side_effect=SideEffectPolicy.IRREVERSIBLE,
        risk_level=RiskLevel.LOW,
        credentials=(
            CredentialRequirement(slot="google", credential_type="google_oauth", required=True),
        ),
        required_permissions=("google.sheets.write",),
    )
    config_model: ClassVar[type[GoogleSheetsAppendRowConfig]] = GoogleSheetsAppendRowConfig

    async def execute(
        self,
        config: GoogleSheetsAppendRowConfig,
        ctx: ExecutionContext,
    ) -> NodeResult:
        cred = await ctx.get_credential("google")
        token = cred.get_secret("token")
        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{config.spreadsheet_id}"
            f"/values/{config.range_name}:append"
        )
        resp = await ctx.http.request(
            "POST",
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={"valueInputOption": "USER_ENTERED"},
            json_body={"values": [config.values]},
        )
        _check_http("google_sheets", resp.status_code, resp.body_text)
        data = resp.body_json if isinstance(resp.body_json, dict) else {}
        return NodeResult(output={"updated_range": data.get("updates", {}).get("updatedRange")})


# 5. Stripe Create Payment Link (Irreversible + Idempotency-Key)
class StripeCreatePaymentLinkConfig(NodeConfigModel):
    price_id: str = Field(..., min_length=4, max_length=128)
    quantity: int = Field(default=1, ge=1, le=1000)


class StripeCreatePaymentLinkNode(BaseNode[StripeCreatePaymentLinkConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="stripe.create_payment_link",
        name="Stripe Payment Link",
        description="Creates a Stripe Checkout payment link (irreversible; sends Idempotency-Key).",
        category="connectors",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.INTEGRATIONS,
        side_effect=SideEffectPolicy.IRREVERSIBLE,
        risk_level=RiskLevel.CRITICAL,
        credentials=(
            CredentialRequirement(slot="stripe", credential_type="stripe_api", required=True),
        ),
        required_permissions=("stripe.payments",),
    )
    config_model: ClassVar[type[StripeCreatePaymentLinkConfig]] = StripeCreatePaymentLinkConfig

    async def execute(
        self,
        config: StripeCreatePaymentLinkConfig,
        ctx: ExecutionContext,
    ) -> NodeResult:
        cred = await ctx.get_credential("stripe")
        secret_key = cred.get_secret("api_key")
        headers = {"Authorization": f"Bearer {secret_key}"}
        if ctx.idempotency_key:
            headers["Idempotency-Key"] = ctx.idempotency_key
        resp = await ctx.http.request(
            "POST",
            "https://api.stripe.com/v1/payment_links",
            headers=headers,
            json_body={"line_items": [{"price": config.price_id, "quantity": config.quantity}]},
        )
        _check_http("stripe", resp.status_code, resp.body_text)
        data = resp.body_json if isinstance(resp.body_json, dict) else {}
        return NodeResult(output={"id": data.get("id"), "url": data.get("url")})


# 6. HubSpot Upsert Contact
class HubSpotUpsertContactConfig(NodeConfigModel):
    email: str = Field(..., min_length=3, max_length=320)
    first_name: str = ""
    last_name: str = ""
    properties: dict[str, Any] = Field(default_factory=dict)


class HubSpotUpsertContactNode(BaseNode[HubSpotUpsertContactConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="hubspot.upsert_contact",
        name="HubSpot Upsert Contact",
        description="Creates or updates a CRM contact record in HubSpot.",
        category="connectors",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.INTEGRATIONS,
        side_effect=SideEffectPolicy.IDEMPOTENT,
        risk_level=RiskLevel.MEDIUM,
        credentials=(
            CredentialRequirement(slot="hubspot", credential_type="hubspot_api", required=True),
        ),
        required_permissions=("hubspot.write",),
    )
    config_model: ClassVar[type[HubSpotUpsertContactConfig]] = HubSpotUpsertContactConfig

    async def execute(
        self,
        config: HubSpotUpsertContactConfig,
        ctx: ExecutionContext,
    ) -> NodeResult:
        cred = await ctx.get_credential("hubspot")
        token = cred.get_secret("token")
        props = {
            "email": config.email,
            "firstname": config.first_name,
            "lastname": config.last_name,
            **config.properties,
        }
        resp = await ctx.http.request(
            "POST",
            "https://api.hubapi.com/crm/v3/objects/contacts",
            headers={"Authorization": f"Bearer {token}"},
            json_body={"properties": props},
        )
        _check_http("hubspot", resp.status_code, resp.body_text)
        data = resp.body_json if isinstance(resp.body_json, dict) else {}
        return NodeResult(output={"id": data.get("id"), "email": config.email})


CONNECTOR_NODES: tuple[type[BaseNode[Any, Any]], ...] = (
    GitHubCreateIssueNode,
    LinearCreateIssueNode,
    NotionCreatePageNode,
    GoogleSheetsAppendRowNode,
    StripeCreatePaymentLinkNode,
    HubSpotUpsertContactNode,
)

CONNECTORS_PACKAGE_MANIFEST = NodePackageManifest(
    package_name="flowpilot-nodes-connectors",
    package_version="0.1.0",
    sdk_compatibility=">=0.1.0,<1.0.0",
    first_party=True,
    entrypoints={
        cls.metadata.type: f"{cls.__module__}:{cls.__name__}" for cls in CONNECTOR_NODES
    },
    permissions=(
        "github.write",
        "linear.write",
        "notion.write",
        "google.sheets.write",
        "stripe.payments",
        "hubspot.write",
    ),
    credential_types=(
        "github_oauth",
        "linear_api",
        "notion_api",
        "google_oauth",
        "stripe_api",
        "hubspot_api",
    ),
)


def register_connector_nodes(registry: NodeRegistry, *, replace: bool = False) -> NodeRegistry:
    for node_cls in CONNECTOR_NODES:
        registry.register(node_cls, replace=replace)
    return registry


__all__ = [
    "CONNECTORS_PACKAGE_MANIFEST",
    "CONNECTOR_NODES",
    "GitHubCreateIssueConfig",
    "GitHubCreateIssueNode",
    "GoogleSheetsAppendRowConfig",
    "GoogleSheetsAppendRowNode",
    "HubSpotUpsertContactConfig",
    "HubSpotUpsertContactNode",
    "LinearCreateIssueConfig",
    "LinearCreateIssueNode",
    "NotionCreatePageConfig",
    "NotionCreatePageNode",
    "StripeCreatePaymentLinkConfig",
    "StripeCreatePaymentLinkNode",
    "register_connector_nodes",
]
