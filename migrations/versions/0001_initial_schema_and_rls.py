"""Initial FlowPilot multi-tenant schema, pgvector support, and PostgreSQL Row-Level Security.

Revision ID: 0001_initial_rls
Revises: None
Create Date: 2026-11-25 00:00:00.000000
"""

from __future__ import annotations

from alembic import op

from flowpilot_api.db.models import TENANT_SCOPED_TABLES, Base

revision = "0001_initial_rls"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
        op.execute('CREATE EXTENSION IF NOT EXISTS "vector"')

    # Create all ORM tables cleanly
    Base.metadata.create_all(bind=bind)

    if is_pg:
        # Add pgvector embedding column to agent_memory for tenant-scoped vector search
        op.execute(
            "ALTER TABLE agent_memory ADD COLUMN IF NOT EXISTS embedding vector(1536)"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_agent_memory_embedding "
            "ON agent_memory USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
        )

        # Enable defense-in-depth PostgreSQL Row-Level Security on all tenant-owned tables
        for table_name in TENANT_SCOPED_TABLES:
            op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")
            op.execute(
                f"""
                CREATE POLICY tenant_isolation_{table_name}
                ON {table_name}
                USING (
                    current_setting('app.current_org_id', true) IS NULL
                    OR current_setting('app.current_org_id', true) = ''
                    OR organization_id =
                        NULLIF(current_setting('app.current_org_id', true), '')::uuid
                )
                WITH CHECK (
                    current_setting('app.current_org_id', true) IS NULL
                    OR current_setting('app.current_org_id', true) = ''
                    OR organization_id =
                        NULLIF(current_setting('app.current_org_id', true), '')::uuid
                )
                """
            )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        for table_name in TENANT_SCOPED_TABLES:
            op.execute(
                f"DROP POLICY IF EXISTS tenant_isolation_{table_name} ON {table_name}"
            )
            op.execute(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY")

    Base.metadata.drop_all(bind=bind)
