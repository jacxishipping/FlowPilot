"""Real PostgreSQL RLS + pgvector integration test (runs in CI with pgvector:pg16 container).

Set FLOWPILOT_TEST_POSTGRES_URL=postgresql+asyncpg://flowpilot:flowpilot@localhost:5432/flowpilot
to execute against a live PostgreSQL instance with pgvector and FORCE ROW LEVEL SECURITY.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

PG_URL = os.environ.get("FLOWPILOT_TEST_POSTGRES_URL", "")


@pytest.mark.skipif(not PG_URL, reason="FLOWPILOT_TEST_POSTGRES_URL not configured")
@pytest.mark.asyncio
async def test_real_postgres_rls_and_pgvector_cosine_search() -> None:
    engine = create_async_engine(PG_URL, future=True)
    try:
        async with engine.begin() as conn:
            # Verify pgvector extension is installed and vector column exists
            ext = (
                await conn.execute(
                    text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
                )
            ).scalar_one_or_none()
            assert ext == "vector"

            # Verify FORCE ROW LEVEL SECURITY is enabled on workflows & credentials
            rls_rows = (
                await conn.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity "
                        "FROM pg_class WHERE relname IN "
                        "('workflows', 'credentials', 'agent_memory')"
                    )
                )
            ).all()
            assert len(rls_rows) == 3
            for _name, rls_on, force_rls in rls_rows:
                assert rls_on is True
                assert force_rls is True

            # Create non-superuser role to verify true RLS row filtering
            await conn.execute(
                text(
                    "DO $$ BEGIN "
                    "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'flowpilot_rls_tester') "
                    "THEN CREATE ROLE flowpilot_rls_tester NOBYPASSRLS; END IF; "
                    "END $$;"
                )
            )
            await conn.execute(
                text("GRANT USAGE ON SCHEMA public TO flowpilot_rls_tester")
            )
            await conn.execute(
                text(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
                    "TO flowpilot_rls_tester"
                )
            )

            org_a = uuid4()
            org_b = uuid4()
            await conn.execute(
                text(
                    "INSERT INTO organizations (id, name, slug, plan, created_at) "
                    "VALUES (:id, :name, :slug, 'free', NOW())"
                ),
                [
                    {"id": org_a, "name": "PG Org A", "slug": f"pg-a-{org_a.hex[:8]}"},
                    {"id": org_b, "name": "PG Org B", "slug": f"pg-b-{org_b.hex[:8]}"},
                ],
            )
            await conn.execute(
                text(
                    "INSERT INTO workspaces (id, organization_id, name, description, created_at) "
                    "VALUES (:id, :org, 'WS A', '', NOW())"
                ),
                {"id": uuid4(), "org": org_a},
            )

            # Switch to non-superuser role with app.current_org_id = org_b -> must see 0 rows!
            await conn.execute(text("SET LOCAL ROLE flowpilot_rls_tester"))
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :org_id, true)"),
                {"org_id": str(org_b)},
            )
            org_b_seen = (
                await conn.execute(
                    text("SELECT count(*) FROM workspaces WHERE organization_id = :org_a"),
                    {"org_a": org_a},
                )
            ).scalar_one()
            assert org_b_seen == 0

            # Switch context to org_a -> must see 1 row!
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :org_id, true)"),
                {"org_id": str(org_a)},
            )
            org_a_seen = (
                await conn.execute(
                    text("SELECT count(*) FROM workspaces WHERE organization_id = :org_a"),
                    {"org_a": org_a},
                )
            ).scalar_one()
            assert org_a_seen == 1

            # Verify pgvector <=> cosine distance operator works natively
            dist = (
                await conn.execute(
                    text("SELECT ('[1,0,0]'::vector <=> '[0,1,0]'::vector) AS cosine_dist")
                )
            ).scalar_one()
            assert abs(float(dist) - 1.0) < 1e-6
    finally:
        await engine.dispose()
