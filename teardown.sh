#!/usr/bin/env bash
# ===========================================================================
# Mediacaster — Teardown Script
# ===========================================================================
#
# Removes the Mediacaster application, database, service user, and all
# associated system configuration. System packages (ffmpeg, Node.js,
# PostgreSQL, Podman, nginx) are left installed.
#
# After running this, use deploy.sh to perform a fresh install.
#
# Usage:   sudo bash teardown.sh
#
# ===========================================================================

set -euo pipefail

# Colors for output
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
log_info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_step()  { echo -e "\n${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; echo -e "${CYAN}  $*${NC}"; echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"; }

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}[ERROR]${NC} This script must be run as root (use sudo)"
    exit 1
fi

echo ""
echo -e "${RED}═══════════════════════════════════════════════════════════${NC}"
echo -e "${RED}  Mediacaster — Full Teardown${NC}"
echo -e "${RED}═══════════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  This will remove:"
echo -e "    • The multicast-streamer service and systemd config"
echo -e "    • All Podman containers and the browser source image"
echo -e "    • The PostgreSQL 'mediacaster' database and 'mcs' user"
echo -e "    • The application directory (/opt/multicast-streamer)"
echo -e "    • The 'mcs' system user"
echo -e "    • nginx and sudoers configuration for Mediacaster"
echo -e "    • NetworkManager multicast dispatcher script"
echo ""
echo -e "  System packages (ffmpeg, Node.js, PostgreSQL, etc.) will ${GREEN}NOT${NC} be removed."
echo ""
read -rp "  Are you sure you want to proceed? (yes/no): " CONFIRM
if [[ "${CONFIRM}" != "yes" ]]; then
    echo ""
    log_warn "Teardown cancelled."
    exit 0
fi
echo ""

# ---------------------------------------------------------------------------
# Step 1: Stop the service
# ---------------------------------------------------------------------------
log_step "Step 1/7: Stopping services"

if systemctl is-active --quiet multicast-streamer 2>/dev/null; then
    systemctl stop multicast-streamer
    log_info "Stopped multicast-streamer service"
else
    log_info "multicast-streamer service not running"
fi

if systemctl is-enabled --quiet multicast-streamer 2>/dev/null; then
    systemctl disable multicast-streamer
    log_info "Disabled multicast-streamer service"
fi

# ---------------------------------------------------------------------------
# Step 2: Remove containers and image
# ---------------------------------------------------------------------------
log_step "Step 2/7: Removing containers and image"

# Stop and remove any running browser source containers
RUNNING=$(podman ps -a --filter "ancestor=mcs-browser-source:latest" -q 2>/dev/null || true)
if [[ -n "${RUNNING}" ]]; then
    podman stop ${RUNNING} 2>/dev/null || true
    podman rm ${RUNNING} 2>/dev/null || true
    log_info "Removed browser source containers"
else
    log_info "No browser source containers found"
fi

if podman image exists mcs-browser-source:latest 2>/dev/null; then
    podman rmi mcs-browser-source:latest
    log_info "Removed container image mcs-browser-source:latest"
else
    log_info "Container image not found — skipping"
fi

# ---------------------------------------------------------------------------
# Step 3: Drop PostgreSQL database and user
# ---------------------------------------------------------------------------
log_step "Step 3/7: Removing database"

if systemctl is-active --quiet postgresql 2>/dev/null; then
    if sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='mediacaster'" 2>/dev/null | grep -q 1; then
        sudo -u postgres psql -c "DROP DATABASE mediacaster;"
        log_info "Dropped database: mediacaster"
    else
        log_info "Database 'mediacaster' does not exist — skipping"
    fi

    if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='mcs'" 2>/dev/null | grep -q 1; then
        sudo -u postgres psql -c "DROP USER mcs;"
        log_info "Dropped database user: mcs"
    else
        log_info "Database user 'mcs' does not exist — skipping"
    fi

    # Clean up pg_hba.conf entries
    PG_HBA="/var/lib/pgsql/data/pg_hba.conf"
    if grep -q "mcs.*mediacaster.*scram-sha-256" "${PG_HBA}" 2>/dev/null; then
        sed -i '/mcs.*mediacaster.*scram-sha-256/d' "${PG_HBA}"
        systemctl restart postgresql
        log_info "Removed pg_hba.conf entries and restarted PostgreSQL"
    fi
else
    log_warn "PostgreSQL not running — skipping database cleanup"
fi

# ---------------------------------------------------------------------------
# Step 4: Remove application directory
# ---------------------------------------------------------------------------
log_step "Step 4/7: Removing application files"

if [[ -d /opt/multicast-streamer ]]; then
    rm -rf /opt/multicast-streamer
    log_info "Removed /opt/multicast-streamer"
else
    log_info "/opt/multicast-streamer does not exist — skipping"
fi

# ---------------------------------------------------------------------------
# Step 5: Remove system configuration
# ---------------------------------------------------------------------------
log_step "Step 5/7: Removing system configuration"

# Systemd unit and overrides (JWT secret, DATABASE_URL)
rm -f /etc/systemd/system/multicast-streamer.service
rm -rf /etc/systemd/system/multicast-streamer.service.d
systemctl daemon-reload
log_info "Removed systemd service and overrides"

# nginx config
rm -f /etc/nginx/conf.d/multicast-streamer.conf
if nginx -t 2>/dev/null; then
    systemctl restart nginx 2>/dev/null || true
    log_info "Removed nginx config and restarted nginx"
else
    log_warn "nginx config test failed — check /etc/nginx/nginx.conf manually"
fi

# Sudoers entry for podman
rm -f /etc/sudoers.d/mcs-podman
log_info "Removed podman sudoers entry"

# NetworkManager multicast dispatcher
rm -f /etc/NetworkManager/dispatcher.d/99-multicast-route
log_info "Removed multicast route dispatcher"

# ---------------------------------------------------------------------------
# Step 6: Remove service user
# ---------------------------------------------------------------------------
log_step "Step 6/7: Removing service user"

if id mcs &>/dev/null; then
    userdel -r mcs 2>/dev/null || userdel mcs 2>/dev/null || true
    log_info "Removed system user: mcs"
else
    log_info "User 'mcs' does not exist — skipping"
fi

# Clean up runtime directory
rm -rf "/run/user/$(id -u mcs 2>/dev/null || echo 'none')" 2>/dev/null || true

# ---------------------------------------------------------------------------
# Step 7: Summary
# ---------------------------------------------------------------------------
log_step "Teardown complete"

echo -e "  All Mediacaster components have been removed."
echo ""
echo -e "  To reinstall from git:"
echo ""
echo -e "    ${CYAN}cd /opt${NC}"
echo -e "    ${CYAN}git clone https://github.com/RobarePruyn/Mediacaster.git mediacaster-src${NC}"
echo -e "    ${CYAN}cd mediacaster-src${NC}"
echo -e "    ${CYAN}sudo bash deploy.sh${NC}"
echo ""
echo -e "  To set a custom admin password:"
echo -e "    ${CYAN}MCS_ADMIN_PASS=yourpass sudo -E bash deploy.sh${NC}"
echo ""
