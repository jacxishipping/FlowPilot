# FlowPilot Self-Hosted Operations & Production Readiness Guide

## 1. Quick Start (Docker Compose)

```bash
cp .env.example .env
# Edit .env and set a high-entropy FLOWPILOT_MASTER_KEY, webhook secrets, and DB passwords
docker compose -f infrastructure/compose/docker-compose.yml up -d --build
```

## 2. Database Migrations & Row-Level Security

Migrations are managed by Alembic (`migrations/versions/0001_initial_schema_and_rls.py`):

```bash
uv run alembic upgrade head
```

Every tenant-scoped table enforces PostgreSQL Row-Level Security (`FORCE ROW LEVEL SECURITY`) against the transaction-local setting `app.current_org_id`.

## 3. Credential Encryption & Key Rotation

Credentials are encrypted per-record with AES-256-GCM DEKs bound to `(organization_id, credential_id)` via AAD. To rotate your Key Encryption Key (KEK) without downtime:

1. Configure both the old and new key IDs in `LocalMasterKeyProvider` (or your Cloud KMS / Vault provider) with `active_key_id` pointing to the new key.
2. Re-wrap stored envelopes using `EnvelopeCipher.rewrap_envelope(...)` without exposing plaintext secrets.

## 4. Backup, Restore & Upgrades

- **Backup**: `./scripts/backup_postgres.sh ./backups` (generates compressed dump + `.sha256` checksum)
- **Restore**: `./scripts/restore_postgres.sh ./backups/flowpilot_<timestamp>.dump` (verifies SHA-256 checksum and restores in a single transaction)
- **Upgrade**: `./scripts/upgrade_self_hosted.sh` (backup -> build -> `alembic upgrade head` -> rolling restart -> `/health` verification)

## 5. Kubernetes Enterprise Deployment

Deploy hardened non-root manifests under `infrastructure/kubernetes/`:

```bash
kubectl apply -f infrastructure/kubernetes/namespace.yaml
kubectl apply -f infrastructure/kubernetes/migration-job.yaml
kubectl apply -f infrastructure/kubernetes/api-deployment.yaml
kubectl apply -f infrastructure/kubernetes/workers-deployment.yaml
```

For untrusted user code execution (`flowpilot-worker-code`), uncomment `runtimeClassName: gvisor` in `workers-deployment.yaml` and apply a strict Kubernetes `NetworkPolicy` blocking all pod egress.
