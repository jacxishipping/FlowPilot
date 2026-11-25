"""FastAPI application factory with middleware, error envelopes, lifespan, and DI wiring."""

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

from flowpilot import NodePackageManifest, NodeRegistry
from flowpilot_agents import AgentToolGateway, InMemoryVectorMemoryStore
from flowpilot_api.config import Settings
from flowpilot_api.db.models import MarketplacePackageModel
from flowpilot_api.db.session import create_engine_and_sessionmaker, init_schema
from flowpilot_api.db.stores import (
    SqlAlchemyCredentialResolver,
    SqlAlchemyEventSink,
    SqlAlchemyExecutionStore,
)
from flowpilot_api.extras_routes import extras_router
from flowpilot_api.routes import router as v1_router
from flowpilot_credentials import EnvelopeCipher, LocalMasterKeyProvider
from flowpilot_engine import WorkflowExecutionEngine
from flowpilot_nodes_core import CORE_PACKAGE_MANIFEST, ModelProvider, create_default_registry
from flowpilot_nodes_database import DATABASE_PACKAGE_MANIFEST, register_database_nodes
from flowpilot_nodes_email import EMAIL_PACKAGE_MANIFEST, register_email_nodes
from flowpilot_nodes_slack import SLACK_PACKAGE_MANIFEST, register_slack_nodes
from flowpilot_observability import MetricsRegistry


def build_full_node_registry() -> NodeRegistry:
    """Create a NodeRegistry populated with all 15 first-party FlowPilot nodes."""
    reg = create_default_registry()
    register_email_nodes(reg)
    register_slack_nodes(reg)
    register_database_nodes(reg)
    return reg


FIRST_PARTY_MANIFESTS: tuple[NodePackageManifest, ...] = (
    CORE_PACKAGE_MANIFEST,
    EMAIL_PACKAGE_MANIFEST,
    SLACK_PACKAGE_MANIFEST,
    DATABASE_PACKAGE_MANIFEST,
)


def create_app(
    settings: Settings | None = None,
    *,
    registry: NodeRegistry | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
    services: dict[str, Any] | None = None,
    agent_model_provider: ModelProvider | None = None,
    tool_gateway: AgentToolGateway | None = None,
) -> FastAPI:
    cfg = settings or Settings()
    node_registry = registry or build_full_node_registry()
    db_engine, session_factory = create_engine_and_sessionmaker(cfg.database_url)
    cipher = EnvelopeCipher(
        LocalMasterKeyProvider(active_key_id=cfg.active_key_id, master_secret=cfg.master_key)
    )
    exec_store = SqlAlchemyExecutionStore(session_factory)
    event_sink = SqlAlchemyEventSink(session_factory)
    cred_resolver = SqlAlchemyCredentialResolver(session_factory, cipher)
    workflow_engine = WorkflowExecutionEngine(
        registry=node_registry,
        store=exec_store,
        event_sink=event_sink,
        credential_resolver=cred_resolver,
        http_transport=http_transport,
        allow_private_networks=cfg.allow_private_networks,
        services=services,
    )
    metrics = MetricsRegistry()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if cfg.auto_create_tables or cfg.database_url.startswith("sqlite"):
            await init_schema(db_engine)
        # Seed all first-party integration packages in marketplace catalog idempotently
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
    app.state.credential_resolver = cred_resolver
    app.state.engine = workflow_engine
    app.state.metrics = metrics
    app.state.agent_model_provider = agent_model_provider
    app.state.tool_gateway = tool_gateway or AgentToolGateway()
    app.state.memory_store = InMemoryVectorMemoryStore()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origin_list(),
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

    app.include_router(v1_router)
    app.include_router(extras_router)
    return app


app = create_app()
