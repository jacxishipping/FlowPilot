"""FastAPI application factory wiring S3, KMS, EventBus, Temporal, Stripe, and OTLP."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select

from flowpilot import (
    MarketplaceSubmissionPipeline,
    NodePackageManifest,
    NodeRegistry,
    PackageTrustPolicy,
    generate_signing_keypair,
)
from flowpilot_agents import (
    AgentToolGateway,
    AnthropicModelProvider,
    GeminiModelProvider,
    MultiModelProvider,
    OllamaModelProvider,
    OpenAIModelProvider,
    SqlAlchemyVectorMemoryStore,
    register_builtin_agent_tools,
)
from flowpilot_api.advanced_routes import advanced_router
from flowpilot_api.billing_service import StripeBillingClient
from flowpilot_api.config import Settings
from flowpilot_api.db.models import MarketplacePackageModel
from flowpilot_api.db.session import create_engine_and_sessionmaker, init_schema
from flowpilot_api.db.stores import (
    SqlAlchemyCredentialResolver,
    SqlAlchemyEventSink,
    SqlAlchemyExecutionStore,
)
from flowpilot_api.enterprise_routes import enterprise_router
from flowpilot_api.extras_routes import extras_router
from flowpilot_api.final_routes import final_router
from flowpilot_api.parity_routes import (
    build_data_table_service,
    build_knowledge_search_service,
    parity_router,
)
from flowpilot_api.platform_routes import platform_router
from flowpilot_api.routes import router as v1_router
from flowpilot_api.temporal_dispatch import TemporalDispatchClient
from flowpilot_credentials import EnvelopeCipher, LocalMasterKeyProvider
from flowpilot_engine import (
    InMemoryArtifactStore,
    InMemoryDeadLetterQueue,
    InMemoryEventBus,
    PayloadManager,
    S3ArtifactStore,
    WorkflowExecutionEngine,
)
from flowpilot_nodes_connectors import CONNECTORS_PACKAGE_MANIFEST, register_connector_nodes
from flowpilot_nodes_core import CORE_PACKAGE_MANIFEST, ModelProvider, create_default_registry
from flowpilot_nodes_database import DATABASE_PACKAGE_MANIFEST, register_database_nodes
from flowpilot_nodes_email import EMAIL_PACKAGE_MANIFEST, register_email_nodes
from flowpilot_nodes_slack import SLACK_PACKAGE_MANIFEST, register_slack_nodes
from flowpilot_observability import MetricsRegistry, OtlpConfig, configure_otlp_tracing
from flowpilot_worker_browser import BrowserAutomationNode
from flowpilot_worker_code import PythonSandboxNode


def build_full_node_registry() -> NodeRegistry:
    """Create a NodeRegistry populated with all 49 first-party FlowPilot nodes."""
    reg = create_default_registry()
    register_email_nodes(reg)
    register_slack_nodes(reg)
    register_database_nodes(reg)
    register_connector_nodes(reg)
    reg.register(PythonSandboxNode)
    reg.register(BrowserAutomationNode)
    return reg


FIRST_PARTY_MANIFESTS: tuple[NodePackageManifest, ...] = (
    CORE_PACKAGE_MANIFEST,
    EMAIL_PACKAGE_MANIFEST,
    SLACK_PACKAGE_MANIFEST,
    DATABASE_PACKAGE_MANIFEST,
    CONNECTORS_PACKAGE_MANIFEST,
)


def _build_default_model_provider(cfg: Settings) -> ModelProvider | None:
    providers: dict[str, ModelProvider] = {}
    if cfg.openai_api_key:
        providers["openai"] = OpenAIModelProvider(api_key=cfg.openai_api_key)
    if cfg.anthropic_api_key:
        providers["anthropic"] = AnthropicModelProvider(api_key=cfg.anthropic_api_key)
    if cfg.gemini_api_key:
        providers["gemini"] = GeminiModelProvider(api_key=cfg.gemini_api_key)
    if cfg.ollama_base_url:
        providers["ollama"] = OllamaModelProvider(base_url=cfg.ollama_base_url)
    return MultiModelProvider(providers) if providers else None


def create_app(
    settings: Settings | None = None,
    *,
    registry: NodeRegistry | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
    oauth_http_transport: httpx.AsyncBaseTransport | None = None,
    s3_transport: httpx.AsyncBaseTransport | None = None,
    stripe_transport: httpx.AsyncBaseTransport | None = None,
    temporal_client: Any | None = None,
    services: dict[str, Any] | None = None,
    agent_model_provider: ModelProvider | None = None,
    tool_gateway: AgentToolGateway | None = None,
) -> FastAPI:
    cfg = settings or Settings()
    configure_otlp_tracing(
        OtlpConfig(
            service_name="flowpilot-api",
            otlp_endpoint=cfg.otlp_endpoint,
            enabled=cfg.otlp_enabled,
        )
    )

    node_registry = registry or build_full_node_registry()
    db_engine, session_factory = create_engine_and_sessionmaker(cfg.database_url)
    cipher = EnvelopeCipher(
        LocalMasterKeyProvider(active_key_id=cfg.active_key_id, master_secret=cfg.master_key)
    )
    event_bus = InMemoryEventBus()
    dlq = InMemoryDeadLetterQueue()
    exec_store = SqlAlchemyExecutionStore(session_factory)
    event_sink = SqlAlchemyEventSink(session_factory, event_bus=event_bus)
    cred_resolver = SqlAlchemyCredentialResolver(session_factory, cipher)

    artifact_store = (
        S3ArtifactStore(
            endpoint_url=cfg.s3_endpoint_url,
            bucket=cfg.s3_bucket,
            access_key=cfg.s3_access_key,
            secret_key=cfg.s3_secret_key,
            transport=s3_transport,
        )
        if cfg.use_s3_artifacts
        else InMemoryArtifactStore()
    )
    payload_manager = PayloadManager(artifact_store=artifact_store)

    resolved_model_provider = agent_model_provider or _build_default_model_provider(cfg)
    data_table_service = build_data_table_service(session_factory)
    knowledge_search_service = build_knowledge_search_service(session_factory)
    engine_services = dict(services or {})
    if resolved_model_provider is not None and "model_provider" not in engine_services:
        engine_services["model_provider"] = resolved_model_provider
    engine_services.setdefault("data_table_service", data_table_service)
    engine_services.setdefault("knowledge_search", knowledge_search_service)

    workflow_engine = WorkflowExecutionEngine(
        registry=node_registry,
        store=exec_store,
        event_sink=event_sink,
        payload_manager=payload_manager,
        credential_resolver=cred_resolver,
        http_transport=http_transport,
        allow_private_networks=cfg.allow_private_networks,
        services=engine_services,
    )
    metrics = MetricsRegistry()
    billing_client = StripeBillingClient(api_key=cfg.stripe_api_key, transport=stripe_transport)
    temporal_dispatch = TemporalDispatchClient(
        address=cfg.temporal_address,
        namespace=cfg.temporal_namespace,
        client=temporal_client,
    )
    priv_key, pub_key = generate_signing_keypair()
    marketplace_pipeline = MarketplaceSubmissionPipeline(
        trust_policy=PackageTrustPolicy(trusted_public_keys=[pub_key]),
        signing_private_key_b64=priv_key,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if cfg.auto_create_tables or cfg.database_url.startswith("sqlite"):
            await init_schema(db_engine)
        async with session_factory() as session:
            for manifest in FIRST_PARTY_MANIFESTS:
                existing = (
                    await session.execute(
                        select(MarketplacePackageModel).where(
                            MarketplacePackageModel.package_name == manifest.package_name
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:
                    session.add(
                        MarketplacePackageModel(
                            package_name=manifest.package_name,
                            package_version=manifest.package_version,
                            publisher="FlowPilot",
                            first_party=True,
                            manifest=manifest.model_dump(mode="json"),
                        )
                    )
            await session.commit()
        yield
        await db_engine.dispose()

    app = FastAPI(
        title="FlowPilot API",
        version="0.1.0",
        description="Multi-tenant workflow automation and bounded AI agent SaaS API",
        lifespan=lifespan,
    )

    app.state.settings = cfg
    app.state.db_engine = db_engine
    app.state.session_factory = session_factory
    app.state.registry = node_registry
    app.state.cipher = cipher
    app.state.execution_store = exec_store
    app.state.event_sink = event_sink
    app.state.event_bus = event_bus
    app.state.dlq = dlq
    app.state.credential_resolver = cred_resolver
    app.state.engine = workflow_engine
    app.state.artifact_store = artifact_store
    app.state.metrics = metrics
    app.state.billing_client = billing_client
    app.state.temporal_dispatch = temporal_dispatch
    app.state.marketplace_pipeline = marketplace_pipeline
    app.state.oauth_http_transport = oauth_http_transport
    app.state.agent_model_provider = resolved_model_provider
    resolved_tool_gateway = tool_gateway or AgentToolGateway()
    register_builtin_agent_tools(resolved_tool_gateway, http_transport=http_transport)
    app.state.tool_gateway = resolved_tool_gateway
    app.state.data_table_service = data_table_service
    app.state.knowledge_search_service = knowledge_search_service
    app.state.memory_store = SqlAlchemyVectorMemoryStore(session_factory)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origin_list(),
        allow_origin_regex=cfg.cors_origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next: Any) -> Any:
        req_id = request.headers.get("x-request-id") or str(uuid4())
        request.state.request_id = req_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = req_id
        metrics.inc(
            "flowpilot_http_requests_total",
            method=request.method,
            status=str(response.status_code),
        )
        return response

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        req_id = getattr(request.state, "request_id", str(uuid4()))
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": f"HTTP_{exc.status_code}",
                    "message": exc.detail if isinstance(exc.detail, str) else "Request failed",
                    "details": exc.detail if not isinstance(exc.detail, str) else {},
                    "request_id": req_id,
                }
            },
            headers={"X-Request-Id": req_id},
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "environment": cfg.environment,
            "execution_mode": cfg.execution_mode,
        }

    app.include_router(final_router)
    app.include_router(parity_router)
    app.include_router(advanced_router)
    app.include_router(enterprise_router)
    app.include_router(platform_router)
    app.include_router(v1_router)
    app.include_router(extras_router)
    return app


app = create_app()
