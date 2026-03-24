#!/usr/bin/env bash
# ===========================================================================
# Multicast Streamer — Update Script
# ===========================================================================
#
# Pulls the latest code from git and applies all changes without running
# a full deploy. Handles: nginx config, frontend rebuild, container image
# rebuild, Python dependencies, and service restart.
#
# Usage:   sudo bash update.sh
#
# Unlike deploy.sh, this script does NOT:
#   - Install system packages or repos
#   - Create users/directories
#   - Initialize PostgreSQL
#   - Configure firewall/SELinux
#   - Generate TLS certificates
#   - Run OS cleanup
#
# ===========================================================================

set -euo pipefail

APP_DIR="/opt/multicast-streamer"
APP_USER="mcs"
APP_GROUP="mcs"

# Colors for output
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
log_info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }
log_step()  { echo -e "\n${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; echo -e "${CYAN}  $*${NC}"; echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"; }

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    log_error "This script must be run as root (use sudo)"
    exit 1
fi

if [[ ! -d "${APP_DIR}/.git" ]]; then
    log_error "No git repository at ${APP_DIR} — run deploy.sh first"
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 1: Pull latest code
# ---------------------------------------------------------------------------
log_step "Step 1/5: Pulling latest code"

cd "${APP_DIR}"
git config --global --add safe.directory "${APP_DIR}" 2>/dev/null || true
git pull origin main
chown -R "${APP_USER}:${APP_GROUP}" "${APP_DIR}"
log_info "Code updated"

# ---------------------------------------------------------------------------
# Step 2: Python dependencies
# ---------------------------------------------------------------------------
log_step "Step 2/5: Updating Python dependencies"

"${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt" --quiet
log_info "Python dependencies up to date"

# ---------------------------------------------------------------------------
# Step 3: Rebuild frontend
# ---------------------------------------------------------------------------
log_step "Step 3/5: Rebuilding frontend"

cd "${APP_DIR}/frontend"
sudo -u "${APP_USER}" npm install --include=dev
sudo -u "${APP_USER}" npm run build
rm -rf node_modules
log_info "Frontend build complete"

# ---------------------------------------------------------------------------
# Step 4: Update nginx config
# ---------------------------------------------------------------------------
log_step "Step 4/5: Updating nginx config"

cp "${APP_DIR}/nginx/multicast-streamer.conf" /etc/nginx/conf.d/
nginx -t
systemctl restart nginx
log_info "nginx restarted with latest config"

# ---------------------------------------------------------------------------
# Step 5: Restart application service
# ---------------------------------------------------------------------------
log_step "Step 5/5: Restarting application"

systemctl daemon-reload
systemctl restart multicast-streamer
sleep 2

if systemctl is-active --quiet multicast-streamer; then
    log_info "multicast-streamer is running"
else
    log_error "multicast-streamer failed — check: journalctl -u multicast-streamer -n 50"
fi

# ---------------------------------------------------------------------------
# Optional: Rebuild container image
# ---------------------------------------------------------------------------
# Check if container files changed in the last commit
if git diff HEAD~1 --name-only 2>/dev/null | grep -q "^container/"; then
    log_step "Container files changed — rebuilding browser source image"
    # Stop any running containers so the old image can be replaced
    podman stop -a 2>/dev/null || true
    podman rm -a 2>/dev/null || true
    cd "${APP_DIR}/container"
    podman build -t mcs-browser-source:latest -f Containerfile . || {
        log_warn "Container image build failed — browser sources may not work"
        log_warn "Rebuild manually: cd ${APP_DIR}/container && sudo podman build -t mcs-browser-source:latest ."
    }
    log_info "Container image rebuilt"
else
    log_info "No container changes detected — skipping image rebuild"
    log_info "To force a rebuild: cd ${APP_DIR}/container && sudo podman build -t mcs-browser-source:latest ."
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
SERVER_IP=$(hostname -I | awk '{print $1}')
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Multicast Streamer — Update Complete${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  Web UI:  ${CYAN}https://${SERVER_IP}/${NC}"
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
