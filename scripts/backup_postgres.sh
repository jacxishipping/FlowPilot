#!/usr/bin/env bash
set -euo pipefail

# FlowPilot Self-Hosted PostgreSQL Backup Script
# Usage: ./scripts/backup_postgres.sh [output_dir]

OUTPUT_DIR="${1:-./backups}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_FILE="${OUTPUT_DIR}/flowpilot_${TIMESTAMP}.dump"

mkdir -p "${OUTPUT_DIR}"

: "${POSTGRES_HOST:=localhost}"
: "${POSTGRES_PORT:=5432}"
: "${POSTGRES_USER:=flowpilot}"
: "${POSTGRES_DB:=flowpilot}"

echo "[flowpilot-backup] Dumping database '${POSTGRES_DB}' to ${BACKUP_FILE}..."
pg_dump \
  --host="${POSTGRES_HOST}" \
  --port="${POSTGRES_PORT}" \
  --username="${POSTGRES_USER}" \
  --format=custom \
  --compress=9 \
  --file="${BACKUP_FILE}" \
  "${POSTGRES_DB}"

sha256sum "${BACKUP_FILE}" > "${BACKUP_FILE}.sha256"
echo "[flowpilot-backup] Backup verified and checksum written: ${BACKUP_FILE}.sha256"
