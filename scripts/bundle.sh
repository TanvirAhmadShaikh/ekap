#!/usr/bin/env bash
# bundle.sh — Package EKAP for deployment on another machine.
#
# Usage:
#   ./scripts/bundle.sh                   # source + custom images (recommended)
#   ./scripts/bundle.sh --all-images      # include third-party images (offline/air-gapped)
#   ./scripts/bundle.sh --with-data       # also export runtime volumes (full migration)
#
# Output: ekap-bundle-YYYYMMDD.tar.gz  (~2.4 GB default, ~12 GB --all-images)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BUNDLE_NAME="ekap-bundle-$(date +%Y%m%d-%H%M)"
BUNDLE_DIR="/tmp/$BUNDLE_NAME"
ALL_IMAGES=false
WITH_DATA=false

for arg in "$@"; do
  case "$arg" in
    --all-images) ALL_IMAGES=true ;;
    --with-data)  WITH_DATA=true  ;;
    *) echo "Unknown option: $arg"; exit 1 ;;
  esac
done

echo "════════════════════════════════════════"
echo "  EKAP Bundle Creator"
echo "  Mode: $([ "$ALL_IMAGES" = true ] && echo 'all images' || echo 'custom images only')"
echo "  Data: $([ "$WITH_DATA"  = true ] && echo 'yes (volume export)' || echo 'no')"
echo "════════════════════════════════════════"
echo

mkdir -p "$BUNDLE_DIR"

# ── 1. Source code ────────────────────────────────────────────────────────────
echo "▸ Copying source code…"
tar -C "$PROJECT_DIR" \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='.env' \
    -cf - . | tar -C "$BUNDLE_DIR" -xf -
echo "  ✓ Source code copied"

# ── 2. Create .env template (strip secrets, keep structure) ──────────────────
if [ -f "$PROJECT_DIR/.env" ]; then
  echo "▸ Creating .env.template (secrets blanked)…"
  sed \
    -e 's/\(PASSWORD\s*=\s*\).*/\1CHANGE_ME/' \
    -e 's/\(SECRET_KEY\s*=\s*\).*/\1CHANGE_ME_32_CHAR_RANDOM_STRING/' \
    -e 's/\(HF_TOKEN\s*=\s*\).*/\1/' \
    "$PROJECT_DIR/.env" > "$BUNDLE_DIR/.env.template"
  echo "  ✓ .env.template created (fill in passwords before deploying)"
fi

# ── 3. Custom Docker images (always included) ─────────────────────────────────
CUSTOM_IMAGES=(
  "ekap-ingestion-service:latest"
  "ekap-retrieval-service:latest"
  "ekap-keycloak-setup:latest"
)

echo "▸ Saving custom Docker images…"
echo "  (ingestion-service + retrieval-service + keycloak-setup)"
docker save "${CUSTOM_IMAGES[@]}" | gzip > "$BUNDLE_DIR/images-custom.tar.gz"
echo "  ✓ Custom images saved: $(du -sh "$BUNDLE_DIR/images-custom.tar.gz" | cut -f1)"

# ── 4. Third-party images (optional) ─────────────────────────────────────────
if [ "$ALL_IMAGES" = true ]; then
  echo "▸ Saving third-party Docker images (this may take a while)…"
  THIRD_PARTY=(
    "postgres:16-alpine"
    "qdrant/qdrant:v1.9.4"
    "quay.io/keycloak/keycloak:24.0"
    "prom/prometheus:v2.52.0"
    "grafana/grafana:10.4.3"
    "nginx:1.27-alpine"
    "python:3.11-slim"
  )
  docker save "${THIRD_PARTY[@]}" | gzip > "$BUNDLE_DIR/images-thirdparty.tar.gz"
  echo "  ✓ Third-party images: $(du -sh "$BUNDLE_DIR/images-thirdparty.tar.gz" | cut -f1)"

  echo "▸ Saving Open WebUI (7 GB — this will take several minutes)…"
  docker save "ghcr.io/open-webui/open-webui:main" | gzip > "$BUNDLE_DIR/images-openwebui.tar.gz"
  echo "  ✓ Open WebUI: $(du -sh "$BUNDLE_DIR/images-openwebui.tar.gz" | cut -f1)"
fi

# ── 5. Runtime data volumes (optional) ───────────────────────────────────────
if [ "$WITH_DATA" = true ]; then
  echo "▸ Exporting runtime volumes…"
  mkdir -p "$BUNDLE_DIR/volumes"

  # PostgreSQL — logical dump (portable across versions)
  echo "  postgres…"
  docker exec ekap-postgres-1 pg_dump -U ekap ekap | \
    gzip > "$BUNDLE_DIR/volumes/postgres-ekap.sql.gz"
  docker exec ekap-postgres-1 pg_dump -U ekap keycloak | \
    gzip > "$BUNDLE_DIR/volumes/postgres-keycloak.sql.gz"

  # Uploaded files (documents)
  echo "  uploads…"
  docker run --rm -v ekap_uploads:/data alpine \
    tar -czf - -C /data . > "$BUNDLE_DIR/volumes/uploads.tar.gz"

  # Qdrant vector store
  echo "  qdrant…"
  docker run --rm -v ekap_qdrant_data:/data alpine \
    tar -czf - -C /data . > "$BUNDLE_DIR/volumes/qdrant.tar.gz"

  # Grafana dashboards/config
  echo "  grafana…"
  docker run --rm -v ekap_grafana_data:/data alpine \
    tar -czf - -C /data . > "$BUNDLE_DIR/volumes/grafana.tar.gz"

  # Open WebUI data (user accounts, conversations)
  echo "  open-webui data…"
  docker run --rm -v ekap_open_webui_data:/data alpine \
    tar -czf - -C /data . > "$BUNDLE_DIR/volumes/open-webui.tar.gz"

  echo "  ✓ Volumes exported: $(du -sh "$BUNDLE_DIR/volumes" | cut -f1)"
fi

# ── 6. Write install script ───────────────────────────────────────────────────
cat > "$BUNDLE_DIR/install.sh" << 'INSTALL_EOF'
#!/usr/bin/env bash
# install.sh — Deploy EKAP on this machine.
# Run as a user with Docker access (member of the 'docker' group).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "════════════════════════════════════════"
echo "  EKAP Installer"
echo "════════════════════════════════════════"
echo

# ── Prerequisites check ───────────────────────────────────────────────────────
for cmd in docker curl; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "✗ '$cmd' not found. Install Docker Engine + Docker Compose v2 first."
    echo "  https://docs.docker.com/engine/install/"
    exit 1
  fi
done
if ! docker compose version &>/dev/null; then
  echo "✗ Docker Compose v2 not found (need 'docker compose', not 'docker-compose')."
  exit 1
fi

# ── Port 80 check ─────────────────────────────────────────────────────────────
if ss -tlnp 2>/dev/null | grep -q ':80 ' || netstat -tlnp 2>/dev/null | grep -q ':80 '; then
  echo "⚠  Port 80 is in use. EKAP nginx is mapped to 8080:80 so this is fine."
  echo "   If port 8080 is also in use, edit nginx ports in docker-compose.yml."
fi

# ── .env setup ────────────────────────────────────────────────────────────────
if [ ! -f .env ]; then
  if [ -f .env.template ]; then
    cp .env.template .env
    echo "▸ Created .env from template."
    echo
    echo "  ⚠  IMPORTANT: Edit .env and set secure passwords before continuing."
    echo "     Required fields: POSTGRES_PASSWORD, WEBUI_SECRET_KEY, KEYCLOAK_ADMIN_PASSWORD, GRAFANA_PASSWORD"
    echo
    read -rp "  Press Enter after editing .env to continue (Ctrl-C to abort)…"
  else
    echo "✗ No .env or .env.template found. Cannot continue."
    exit 1
  fi
fi

# ── Load Docker images ────────────────────────────────────────────────────────
echo "▸ Loading custom Docker images…"
gunzip -c images-custom.tar.gz | docker load
echo "  ✓ Custom images loaded"

if [ -f images-thirdparty.tar.gz ]; then
  echo "▸ Loading third-party images (offline mode)…"
  gunzip -c images-thirdparty.tar.gz | docker load
  if [ -f images-openwebui.tar.gz ]; then
    echo "▸ Loading Open WebUI image…"
    gunzip -c images-openwebui.tar.gz | docker load
  fi
  echo "  ✓ All images loaded"
else
  echo "▸ Third-party images will be pulled from the internet (postgres, qdrant, keycloak, etc.)"
fi

# ── Restore volumes (if data export included) ─────────────────────────────────
if [ -d volumes ]; then
  echo "▸ Restoring volume data…"

  docker volume create ekap_postgres_data   2>/dev/null || true
  docker volume create ekap_qdrant_data     2>/dev/null || true
  docker volume create ekap_uploads         2>/dev/null || true
  docker volume create ekap_grafana_data    2>/dev/null || true
  docker volume create ekap_open_webui_data 2>/dev/null || true
  docker volume create ekap_prometheus_data 2>/dev/null || true
  docker volume create ekap_vllm_models     2>/dev/null || true

  # Restore file volumes
  for vol in uploads qdrant grafana open-webui; do
    file="volumes/${vol}.tar.gz"
    [ -f "$file" ] || continue
    vname="ekap_${vol//-/_}_data"
    # map name → volume
    case "$vol" in
      uploads)    vname="ekap_uploads" ;;
      qdrant)     vname="ekap_qdrant_data" ;;
      grafana)    vname="ekap_grafana_data" ;;
      open-webui) vname="ekap_open_webui_data" ;;
    esac
    echo "  restoring $vname…"
    docker run --rm -v "$vname:/data" -i alpine tar -xzf - -C /data < "$file"
  done

  # Restore PostgreSQL after postgres container is started below
  RESTORE_PG=true
else
  RESTORE_PG=false
fi

# ── Start services ────────────────────────────────────────────────────────────
echo "▸ Starting EKAP services…"
if [ "$RESTORE_PG" = true ]; then
  # Start only postgres first so we can restore the dump
  docker compose up -d postgres
  echo "  Waiting for postgres to be healthy…"
  until docker compose exec -T postgres pg_isready -U ekap &>/dev/null; do sleep 2; done

  echo "  Restoring PostgreSQL databases…"
  if [ -f volumes/postgres-keycloak.sql.gz ]; then
    docker compose exec -T postgres psql -U ekap -c "CREATE DATABASE keycloak;" 2>/dev/null || true
    gunzip -c volumes/postgres-keycloak.sql.gz | \
      docker compose exec -T postgres psql -U ekap -d keycloak
  fi
  gunzip -c volumes/postgres-ekap.sql.gz | \
    docker compose exec -T postgres psql -U ekap -d ekap
  echo "  ✓ Databases restored"
fi

docker compose up -d
echo
echo "════════════════════════════════════════"
echo "  EKAP is starting up!"
echo "════════════════════════════════════════"
echo
echo "  Open WebUI downloads sentence-transformers on first boot (~2 min)."
echo "  Check status: docker compose ps"
echo "  View logs:    docker compose logs -f"
echo
echo "  Once healthy, access at:"
echo "    Employee portal  →  http://$(hostname -I | awk '{print $1}'):8080/portal/"
echo "    Admin portal     →  http://$(hostname -I | awk '{print $1}'):8080/admin/"
echo "    Chat (OpenWebUI) →  http://$(hostname -I | awk '{print $1}'):8080/"
echo "    Keycloak admin   →  http://$(hostname -I | awk '{print $1}'):8080/auth/admin"
echo "    Grafana          →  http://$(hostname -I | awk '{print $1}'):8080/grafana/"
echo
INSTALL_EOF
chmod +x "$BUNDLE_DIR/install.sh"

# ── 7. Write README ───────────────────────────────────────────────────────────
cat > "$BUNDLE_DIR/DEPLOY.md" << 'README_EOF'
# EKAP Deployment Bundle

## Requirements (target machine)
- Linux (Ubuntu 22.04+ recommended) or macOS
- Docker Engine 24+ and Docker Compose v2
- 4 GB RAM minimum (8 GB recommended)
- 30 GB free disk space

## Quick start

```bash
# 1. Extract the bundle
tar -xzf ekap-bundle-*.tar.gz
cd ekap-bundle-*/

# 2. Edit passwords
cp .env.template .env
nano .env          # set POSTGRES_PASSWORD, WEBUI_SECRET_KEY, etc.

# 3. Run the installer
chmod +x install.sh
./install.sh

# 4. Wait ~3 minutes, then open:
#   http://<server-ip>:8080/portal/   ← employees
#   http://<server-ip>:8080/admin/    ← admins
```

## GPU (LLM acceleration)
To run vLLM with a GPU:
```bash
docker compose --profile gpu up -d
```
Set `VLLM_MODEL` in `.env` to your chosen Hugging Face model ID.

## Ports
| Service        | Port   |
|----------------|--------|
| Everything     | 8080   |
| PostgreSQL     | 5432 (internal only) |

README_EOF

# ── 8. Create the final tarball ───────────────────────────────────────────────
echo
echo "▸ Creating final archive…"
OUTPUT="$(pwd)/$BUNDLE_NAME.tar.gz"
tar -C /tmp -czf "$OUTPUT" "$BUNDLE_NAME"
rm -rf "$BUNDLE_DIR"

FINAL_SIZE=$(du -sh "$OUTPUT" | cut -f1)
echo
echo "════════════════════════════════════════"
echo "  Bundle created: $BUNDLE_NAME.tar.gz"
echo "  Size: $FINAL_SIZE"
echo "════════════════════════════════════════"
echo
echo "  Copy to target machine, then:"
echo "    tar -xzf $BUNDLE_NAME.tar.gz"
echo "    cd $BUNDLE_NAME"
echo "    ./install.sh"
echo
