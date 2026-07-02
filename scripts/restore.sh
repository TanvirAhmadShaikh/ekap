#!/usr/bin/env bash
# EKAP restore from a backup archive produced by backup.sh.
# Usage: ./scripts/restore.sh <backup-archive.tar.gz>
# WARNING: this OVERWRITES the running databases. Bring services down first.

set -euo pipefail

ARCHIVE="${1:-}"
if [[ -z "${ARCHIVE}" || ! -f "${ARCHIVE}" ]]; then
  echo "Usage: $0 <backup-archive.tar.gz>" >&2
  exit 1
fi

cd "$(dirname "$0")/.."

echo "=== EKAP Restore: ${ARCHIVE} ==="
echo "WARNING: This will overwrite the current databases."
echo "         Ensure docker compose is running (postgres, qdrant)."
read -rp "Type 'yes' to continue: " CONFIRM
[[ "${CONFIRM}" == "yes" ]] || { echo "Aborted."; exit 0; }

WORK_DIR=$(mktemp -d)
trap 'rm -rf "${WORK_DIR}"' EXIT

echo "[1/4] Extracting archive..."
tar xzf "${ARCHIVE}" -C "${WORK_DIR}" --strip-components=1

# ── PostgreSQL ──────────────────────────────────────────────────────────────

echo "[2/4] Restoring PostgreSQL..."
for DB in ekap keycloak; do
  DUMP="${WORK_DIR}/postgres-${DB}.dump"
  [[ -f "${DUMP}" ]] || { echo "      WARN: ${DUMP} not found, skipping."; continue; }

  # Drop all connections, then drop+recreate DB
  docker compose exec -T postgres psql -U "${POSTGRES_USER:-ekap}" -d postgres -c \
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='${DB}' AND pid<>pg_backend_pid();" \
    > /dev/null

  docker compose exec -T postgres psql -U "${POSTGRES_USER:-ekap}" -d postgres \
    -c "DROP DATABASE IF EXISTS ${DB}; CREATE DATABASE ${DB};" > /dev/null

  docker compose exec -T postgres pg_restore \
    -U "${POSTGRES_USER:-ekap}" \
    -d "${DB}" \
    --no-owner \
    --role="${POSTGRES_USER:-ekap}" \
    < "${DUMP}"

  echo "      ${DB} restored."
done

# ── Qdrant ──────────────────────────────────────────────────────────────────

echo "[3/4] Restoring Qdrant snapshots..."
QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"

for COLLECTION in ekap_chunks ekap_sections; do
  SNAP="${WORK_DIR}/qdrant-${COLLECTION}.snapshot"
  [[ -f "${SNAP}" ]] || { echo "      WARN: ${SNAP} not found, skipping."; continue; }

  # Upload snapshot file to Qdrant
  UPLOAD_RESP=$(curl -sf -X POST \
    "${QDRANT_URL}/collections/${COLLECTION}/snapshots/upload?priority=snapshot" \
    -H "Content-Type: multipart/form-data" \
    -F "snapshot=@${SNAP}")
  echo "      ${COLLECTION}: ${UPLOAD_RESP}"
done

# ── Uploads volume ───────────────────────────────────────────────────────────

echo "[4/4] Restoring uploaded files..."
UPLOADS_TAR="${WORK_DIR}/uploads.tar.gz"
if [[ -f "${UPLOADS_TAR}" ]]; then
  docker run --rm \
    -v ekap_uploads:/data \
    -v "$(realpath "${WORK_DIR}"):/src:ro" \
    alpine sh -c "rm -rf /data/* && tar xzf /src/uploads.tar.gz -C /data"
  echo "      Uploads restored."
else
  echo "      WARN: uploads.tar.gz not found, skipping."
fi

echo ""
echo "=== Restore complete ==="
echo "    Restart services: docker compose up -d"
