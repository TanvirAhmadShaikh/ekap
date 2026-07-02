#!/usr/bin/env bash
# EKAP backup: PostgreSQL dump + Qdrant snapshots → timestamped archive.
# Usage: ./scripts/backup.sh [output-dir]
# Requires: docker compose running, pg_dump inside postgres container.

set -euo pipefail

BACKUP_DIR="${1:-./backups}"
TS=$(date -u +"%Y%m%dT%H%M%SZ")
TARGET="${BACKUP_DIR}/${TS}"
mkdir -p "${TARGET}"

cd "$(dirname "$0")/.."

echo "=== EKAP Backup: ${TS} ==="

# ── PostgreSQL ──────────────────────────────────────────────────────────────

echo "[1/3] Dumping PostgreSQL (ekap database)..."
docker compose exec -T postgres pg_dump \
  -U "${POSTGRES_USER:-ekap}" \
  -d "${POSTGRES_DB:-ekap}" \
  --format=custom \
  --compress=9 \
  > "${TARGET}/postgres-ekap.dump"

echo "      Dumping Keycloak database..."
docker compose exec -T postgres pg_dump \
  -U "${POSTGRES_USER:-ekap}" \
  -d keycloak \
  --format=custom \
  --compress=9 \
  > "${TARGET}/postgres-keycloak.dump"

echo "      PostgreSQL done."

# ── Qdrant ──────────────────────────────────────────────────────────────────

echo "[2/3] Creating Qdrant snapshots..."
QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"

for COLLECTION in ekap_chunks ekap_sections; do
  SNAPSHOT_RESP=$(curl -sf -X POST "${QDRANT_URL}/collections/${COLLECTION}/snapshots")
  SNAPSHOT_NAME=$(echo "${SNAPSHOT_RESP}" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['name'])")
  echo "      Snapshot created: ${SNAPSHOT_NAME}"

  # Download it
  curl -sf "${QDRANT_URL}/collections/${COLLECTION}/snapshots/${SNAPSHOT_NAME}" \
    -o "${TARGET}/qdrant-${COLLECTION}.snapshot"

  # Clean up server-side snapshot to avoid filling the container disk
  curl -sf -X DELETE "${QDRANT_URL}/collections/${COLLECTION}/snapshots/${SNAPSHOT_NAME}" > /dev/null

  echo "      ${COLLECTION} snapshot saved."
done

# ── Upload volume ────────────────────────────────────────────────────────────

echo "[3/3] Archiving uploaded files..."
docker run --rm \
  -v ekap_uploads:/data \
  -v "$(realpath "${TARGET}"):/out" \
  alpine tar czf /out/uploads.tar.gz -C /data .

echo "      Uploads archived."

# ── Manifest ────────────────────────────────────────────────────────────────

cat > "${TARGET}/manifest.json" <<EOF
{
  "timestamp": "${TS}",
  "components": {
    "postgres_ekap":     "postgres-ekap.dump",
    "postgres_keycloak": "postgres-keycloak.dump",
    "qdrant_chunks":     "qdrant-ekap_chunks.snapshot",
    "qdrant_sections":   "qdrant-ekap_sections.snapshot",
    "uploads":           "uploads.tar.gz"
  }
}
EOF

# ── Final archive ────────────────────────────────────────────────────────────

ARCHIVE="${BACKUP_DIR}/ekap-backup-${TS}.tar.gz"
tar czf "${ARCHIVE}" -C "${BACKUP_DIR}" "${TS}"
rm -rf "${TARGET}"

SIZE=$(du -sh "${ARCHIVE}" | cut -f1)
echo ""
echo "=== Backup complete ==="
echo "    Archive: ${ARCHIVE}"
echo "    Size:    ${SIZE}"
