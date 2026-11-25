"""Runtime configuration for FlowPilot FastAPI service."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FLOWPILOT_",
        env_file=".env",
        extra="ignore",
    )

    environment: str = "development"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    database_url: str = "postgresql+asyncpg://flowpilot:flowpilot@localhost:5432/flowpilot"
    redis_url: str = "redis://localhost:6379/0"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    execution_mode: str = "local"  # 'local' or 'temporal'
    master_key: str = "flowpilot-dev-only-insecure-master-key-change-me"
    active_key_id: str = "local-v1"
    webhook_signing_secret: str = "flowpilot-dev-webhook-secret-change-me"
    stripe_webhook_secret: str = "whsec_flowpilot_dev_change_me"
    s3_endpoint_url: str = "http://localhost:9000"
    s3_bucket: str = "flowpilot-artifacts"
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    allow_private_networks: bool = False
    auto_create_tables: bool = False

    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]
