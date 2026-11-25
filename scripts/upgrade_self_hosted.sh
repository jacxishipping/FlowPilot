#!/usr/bin/env bash
set -euo pipefail

# FlowPilot Self-Hosted Zero-Surprise Upgrade Procedure
# 1. Take verified PostgreSQL backup
# 2. Pull / rebuild latest containers
# 3. Apply Alembic migrations cleanly
# 4. Restart API, workers, and web UI
# 5. Verify /health endpoint

COMPOSE_FILE="${COMPOSE_FILE:-infrastructure/compose/docker-compose.yml}"

echo "[flowpilot-upgrade] Step 1/5: Taking pre-upgrade PostgreSQL backup..."
./scripts/backup_postgres.sh ./backups

echo "[flowpilot-upgrade] Step 2/5: Building updated images..."
docker compose -f "${COMPOSE_FILE}" build api worker-workflow worker-integrations web

echo "[flowpilot-upgrade] Step 3/5: Applying Alembic migrations..."
docker compose -f "${COMPOSE_FILE}" run --rm api uv run alembic upgrade head

echo "[flowpilot-upgrade] Step 4/5: Restarting services..."
docker compose -f "${COMPOSE_FILE}" up -d api worker-workflow worker-integrations web

echo "[flowpilot-upgrade] Step 5/5: Checking API health..."
curl -fsS http://localhost:8000/health
echo ""
echo "[flowpilot-upgrade] Upgrade complete."
