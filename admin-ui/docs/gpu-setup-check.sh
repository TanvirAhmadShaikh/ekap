#!/usr/bin/env bash
# gpu-setup-check.sh — EKAP hardware/GPU diagnostic.
#
# Run this ON THE MACHINE THAT HOSTS DOCKER/OLLAMA for your EKAP deployment
# (not inside a container — Docker hides the real host from containers, which
# is why this can't just be a button in the admin UI). It detects your OS,
# GPU, and Docker GPU-passthrough setup, then prints exact next steps.
#
# Usage:
#   chmod +x gpu-setup-check.sh
#   ./gpu-setup-check.sh
set -uo pipefail

BOLD='\033[1m'; DIM='\033[2m'; GREEN='\033[32m'; YELLOW='\033[33m'; RED='\033[31m'; RESET='\033[0m'
ok()   { echo -e "  ${GREEN}✓${RESET} $1"; }
warn() { echo -e "  ${YELLOW}⚠${RESET} $1"; }
bad()  { echo -e "  ${RED}✗${RESET} $1"; }
info() { echo -e "  ${DIM}·${RESET} $1"; }
section() { echo; echo -e "${BOLD}$1${RESET}"; }

echo "════════════════════════════════════════════════════"
echo "  EKAP Hardware / GPU Setup Check"
echo "════════════════════════════════════════════════════"

OS="$(uname -s)"
ARCH="$(uname -m)"
IS_WSL=false
if [ "$OS" = "Linux" ] && grep -qi microsoft /proc/version 2>/dev/null; then
  IS_WSL=true
fi

section "System"
info "OS: $OS ($ARCH)$([ "$IS_WSL" = true ] && echo ' — WSL2')"
if command -v free >/dev/null 2>&1; then
  info "RAM: $(free -h | awk '/^Mem:/{print $2 " total, " $7 " available"}')"
elif [ "$OS" = "Darwin" ]; then
  info "RAM: $(( $(sysctl -n hw.memsize) / 1024 / 1024 / 1024 )) GB total"
fi
if command -v docker >/dev/null 2>&1; then
  ok "Docker found: $(docker --version)"
else
  bad "Docker not found on PATH. Install Docker Engine + Compose v2 first: https://docs.docker.com/engine/install/"
fi

RECOMMENDATION=""

# ── macOS ─────────────────────────────────────────────────────────────────────
if [ "$OS" = "Darwin" ]; then
  section "GPU"
  if [ "$ARCH" = "arm64" ]; then
    ok "Apple Silicon detected."
    warn "Docker Desktop on macOS has NO NVIDIA GPU passthrough — the docker-compose"
    warn "'--profile gpu' (vLLM/CUDA) service will NEVER work here, on any Mac."
    RECOMMENDATION=$(cat <<'EOF'
Run Ollama NATIVELY on macOS (not in Docker) — it uses Apple's Metal API
directly and will use the GPU automatically on Apple Silicon:

  1. Install: https://ollama.com/download
  2. Start it:                ollama serve
  3. Pull a model:            ollama pull llama3.2:3b
  4. Check .env has:          LLM_BASE_URL=http://host.docker.internal:11435/v1
     (confirm the PORT matches what Ollama is actually listening on — its
     default is 11434; EKAP's default assumes 11435, so either run
     `OLLAMA_HOST=127.0.0.1:11435 ollama serve` or update LLM_BASE_URL to match)
  5. Start the stack WITHOUT the gpu profile:
                               docker compose up -d
EOF
)
  else
    ok "Intel Mac detected."
    bad "No viable local-GPU path exists for LLM inference on Intel Macs (no CUDA, no ROCm,"
    bad "and Metal-compute LLM runtimes aren't practical here)."
    RECOMMENDATION=$(cat <<'EOF'
Run Ollama natively in CPU mode (still faster than Docker's virtualization
overhead) and pick a small model:

  1. Install: https://ollama.com/download
  2. Pull a small model:      ollama pull llama3.2:3b   (or a smaller/quantized one)
  3. Start the stack WITHOUT the gpu profile:
                               docker compose up -d

For real GPU acceleration, use a Linux machine with an NVIDIA GPU, or a
GPU-backed cloud instance, instead of this Mac.
EOF
)
  fi

# ── WSL2 ──────────────────────────────────────────────────────────────────────
elif [ "$IS_WSL" = true ]; then
  section "GPU"
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    ok "nvidia-smi works inside WSL2 — Windows-side NVIDIA driver with WSL support is installed."
    if docker info 2>/dev/null | grep -qi nvidia; then
      ok "Docker daemon has the nvidia runtime registered."
      RECOMMENDATION="Everything looks correct. Run: docker compose --profile gpu up -d"
    else
      warn "Docker doesn't see an nvidia runtime yet."
      RECOMMENDATION=$(cat <<'EOF'
Install the NVIDIA Container Toolkit INSIDE this WSL2 distro:

  distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -s -L https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
  sudo apt update && sudo apt install -y nvidia-container-toolkit
  sudo nvidia-ctk runtime configure --runtime=docker
  sudo systemctl restart docker

Then verify:  docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
And retry:    docker compose --profile gpu up -d
EOF
)
    fi
  else
    bad "nvidia-smi not found or failed inside WSL2."
    RECOMMENDATION=$(cat <<'EOF'
GPU passthrough into WSL2 needs the NVIDIA driver installed on WINDOWS
(not inside WSL — do not install the Linux .run driver here):

  1. On Windows, install the latest NVIDIA driver (WSL-enabled, no separate
     "WSL driver" needed for reasonably recent drivers): https://www.nvidia.com/Download/index.aspx
  2. Restart WSL from PowerShell:   wsl --shutdown
  3. Re-open your WSL terminal and re-run this script.
  4. Once nvidia-smi works here, install the NVIDIA Container Toolkit inside
     WSL2 (re-run this script — it'll print those steps once the driver is OK).

If you don't have an NVIDIA GPU on this machine, run without the gpu profile
and use Ollama on CPU instead (see: docker compose up -d, no --profile).
EOF
)
  fi

# ── Linux (bare metal / VM, non-WSL) ───────────────────────────────────────────
elif [ "$OS" = "Linux" ]; then
  section "GPU"
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    ok "NVIDIA driver found:"
    nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader | sed 's/^/      /'
    if command -v nvidia-ctk >/dev/null 2>&1 || dpkg -l 2>/dev/null | grep -qi nvidia-container-toolkit || rpm -q nvidia-container-toolkit >/dev/null 2>&1; then
      ok "NVIDIA Container Toolkit appears to be installed."
      if docker info 2>/dev/null | grep -qi nvidia; then
        ok "Docker daemon has the nvidia runtime registered."
        RECOMMENDATION="Everything looks correct. Run: docker compose --profile gpu up -d"
      else
        warn "Toolkit is installed but Docker doesn't see the nvidia runtime yet."
        RECOMMENDATION=$(cat <<'EOF'
Register the runtime with Docker and restart it:

  sudo nvidia-ctk runtime configure --runtime=docker
  sudo systemctl restart docker

Then verify:  docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
And retry:    docker compose --profile gpu up -d
EOF
)
      fi
    else
      bad "NVIDIA Container Toolkit not found — this is why 'docker compose --profile gpu up'"
      bad "fails with: could not select device driver \"nvidia\" with capabilities: [[gpu]]"
      RECOMMENDATION=$(cat <<'EOF'
Install the NVIDIA Container Toolkit:

  distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -s -L https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
  sudo apt update && sudo apt install -y nvidia-container-toolkit
  sudo nvidia-ctk runtime configure --runtime=docker
  sudo systemctl restart docker

Then verify:  docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
And retry:    docker compose --profile gpu up -d
EOF
)
    fi
  else
    warn "No NVIDIA GPU detected (nvidia-smi missing or failed)."
    RECOMMENDATION=$(cat <<'EOF'
No NVIDIA GPU available — run on CPU via Ollama on the host instead of the
Docker gpu profile:

  1. Install Ollama:           curl -fsSL https://ollama.com/install.sh | sh
  2. Pull a small model:       ollama pull llama3.2:3b
  3. Start the stack WITHOUT the gpu profile:
                                docker compose up -d

If this machine DOES have an AMD GPU, vLLM's ROCm image is a separate,
untested path for this deployment — not covered by this script.
EOF
)
  fi
else
  warn "Unrecognized OS '$OS' — manual setup required."
fi

# ── Current EKAP config, if run from the project directory ────────────────────
if [ -f .env ]; then
  section "Current EKAP .env"
  grep -E '^(LLM_BASE_URL|OLLAMA_MODEL|VLLM_MODEL)=' .env | sed 's/^/  /' || info "(no LLM_BASE_URL/OLLAMA_MODEL/VLLM_MODEL set — using defaults)"
fi

section "Recommendation"
echo "$RECOMMENDATION"
echo
