"""Add collaborative workflow_locks, custom_roles, and user_mfa tables with PostgreSQL RLS.

Revision ID: 0003_locks_mfa_roles
Revises: 0002_n8n_parity
Create Date: 2026-11-25 16:30:00.000000
"""

from __future__ import annotations

from alembic import op

from flowpilot_api.db.models import CustomRoleModel, UserMfaModel, WorkflowLockModel

revision = "0003_locks_mfa_roles"
down_revision = "0002_n8n_parity"
branch_labels = None
depends_on = None

NEW_TABLES = (
    WorkflowLockModel.__table__,
    CustomRoleModel.__table__,
    UserMfaModel.__table__,
)


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    for tbl in NEW_TABLES:
        tbl.create(bind=bind, checkfirst=True)

    if is_pg:
        for tbl in NEW_TABLES:
            table_name = tbl.name
            op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")
            op.execute(
                f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_policies
                        WHERE tablename = '{table_name}'
                          AND policyname = 'tenant_isolation_{table_name}'
                    ) THEN
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
                        );
                    END IF;
                END $$;
                """
            )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    for tbl in reversed(NEW_TABLES):
        if is_pg:
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{tbl.name} ON {tbl.name}")
            op.execute(f"ALTER TABLE {tbl.name} DISABLE ROW LEVEL SECURITY")
        tbl.drop(bind=bind, checkfirst=True)
