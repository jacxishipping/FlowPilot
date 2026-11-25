#!/usr/bin/env bash
set -euo pipefail

# FlowPilot Self-Hosted PostgreSQL Restore & Verification Script
# Usage: ./scripts/restore_postgres.sh <backup_file.dump>

BACKUP_FILE="${1:?Usage: restore_postgres.sh <backup_file.dump>}"

if [ -f "${BACKUP_FILE}.sha256" ]; then
  echo "[flowpilot-restore] Verifying SHA-256 checksum..."
  sha256sum -c "${BACKUP_FILE}.sha256"
fi

: "${POSTGRES_HOST:=localhost}"
: "${POSTGRES_PORT:=5432}"
: "${POSTGRES_USER:=flowpilot}"
: "${POSTGRES_DB:=flowpilot}"

echo "[flowpilot-restore] Restoring ${BACKUP_FILE} into '${POSTGRES_DB}'..."
pg_restore \
  --host="${POSTGRES_HOST}" \
  --port="${POSTGRES_PORT}" \
  --username="${POSTGRES_USER}" \
  --dbname="${POSTGRES_DB}" \
  --clean \
  --if-exists \
  --single-transaction \
  "${BACKUP_FILE}"

echo "[flowpilot-restore] Restore completed cleanly."
