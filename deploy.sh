#!/usr/bin/env bash
# ===========================================================================
# Multicast Streamer — AlmaLinux Deployment Script
# ===========================================================================
#
# Takes a fresh AlmaLinux 8, 9, or 10 server and deploys the complete stack:
#   ffmpeg, Python 3, Node.js 20, nginx, systemd service, firewall, SELinux
#
# This script is idempotent — safe to re-run on an already-deployed server.
# It will skip steps that are already complete (existing user, installed
# packages, etc.) and overwrite config files with the latest versions.
#
# Usage:   sudo bash deploy.sh
# Custom:  MCS_ADMIN_PASS=secret sudo -E bash deploy.sh
#
# ===========================================================================

set -euo pipefail

APP_DIR="/opt/multicast-streamer"
APP_USER="mcs"
APP_GROUP="mcs"
# Python version varies by AlmaLinux release: AL8=3.11, AL9=3.9/3.11, AL10=3.12.
# We auto-detect after package install rather than hardcoding, since the
# venv must use whatever python3 binary dnf actually installed.
NODE_MAJOR_VERSION="20"

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

# Detect AlmaLinux version — used to select the right repo names and
# RPM Fusion release package URLs (they differ per major version)
if [[ -f /etc/almalinux-release ]]; then
    ALMA_VERSION=$(rpm -E %{rhel})
    log_info "Detected AlmaLinux ${ALMA_VERSION}"
else
    log_warn "Not AlmaLinux — proceeding anyway"
    ALMA_VERSION=$(rpm -E %{rhel} 2>/dev/null || echo "9")
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Step 1: Repositories
# ---------------------------------------------------------------------------
log_step "Step 1/10: Enabling repositories"

# EPEL (Extra Packages for Enterprise Linux) provides packages not in the
# base RHEL repos, including x11vnc, xdotool, and other dependencies
dnf install -y epel-release

# CRB/PowerTools unlocks development headers and libraries needed to build
# Python packages with native extensions (e.g., psutil, bcrypt)
if [[ "$ALMA_VERSION" == "8" ]]; then
    # AlmaLinux 8 calls it "PowerTools" (CentOS 8 naming)
    dnf config-manager --set-enabled powertools
    log_info "Enabled PowerTools (AlmaLinux 8)"
else
    # AlmaLinux 9+ adopted the RHEL naming: "CRB" (CodeReady Builder)
    dnf config-manager --set-enabled crb
    log_info "Enabled CRB (AlmaLinux ${ALMA_VERSION})"
fi

# RPM Fusion provides ffmpeg and multimedia codecs that Red Hat/AlmaLinux
# cannot ship due to software patent concerns (H.264, AAC, etc.)
dnf install -y \
    "https://mirrors.rpmfusion.org/free/el/rpmfusion-free-release-${ALMA_VERSION}.noarch.rpm" \
    "https://mirrors.rpmfusion.org/nonfree/el/rpmfusion-nonfree-release-${ALMA_VERSION}.noarch.rpm" \
    || log_warn "RPM Fusion may already be installed"

# ---------------------------------------------------------------------------
# Step 2: System packages
# ---------------------------------------------------------------------------
log_step "Step 2/10: Installing system packages"

log_info "Installing core packages (ffmpeg, Python, PostgreSQL, nginx)..."
dnf install -y \
    ffmpeg ffmpeg-devel \
    python3 python3-pip python3-devel \
    postgresql-server postgresql \
    nginx gcc make git curl \
    poppler-utils

# LibreOffice is needed for presentation-to-slide conversion (headless mode).
# On AL10+ it's not in the standard repos, so we install from the official RPM bundle.
if ! command -v libreoffice &>/dev/null; then
    log_info "Installing LibreOffice (presentation converter)..."
    if dnf install -y libreoffice-impress 2>/dev/null; then
        log_info "LibreOffice installed from system repos"
    else
        log_info "LibreOffice not in repos — installing from official RPM bundle..."
        LO_VERSION="26.2.1"
        LO_FILE="LibreOffice_${LO_VERSION}_Linux_x86-64_rpm.tar.gz"
        LO_URL="https://download.documentfoundation.org/libreoffice/stable/${LO_VERSION}/rpm/x86_64/${LO_FILE}"
        cd /tmp
        curl -L -o "${LO_FILE}" "${LO_URL}"
        tar xzf "${LO_FILE}"
        cd LibreOffice_${LO_VERSION}*_rpm/RPMS/
        dnf install -y ./*.rpm
        # Create a stable symlink so the app can use /usr/bin/libreoffice
        LO_BIN=$(ls /usr/bin/libreoffice* 2>/dev/null | head -1)
        if [[ -n "${LO_BIN}" && "${LO_BIN}" != "/usr/bin/libreoffice" ]]; then
            ln -sf "${LO_BIN}" /usr/bin/libreoffice
        fi
        rm -rf /tmp/LibreOffice_${LO_VERSION}* /tmp/${LO_FILE}
        cd "${APP_DIR}"
        log_info "LibreOffice installed from official bundle"
    fi
else
    log_info "LibreOffice already installed: $(libreoffice --version 2>&1 | head -1)"
fi

# Wayland capture pipeline — native process-based browser/presentation source capture.
# Replaces the Podman container approach for lower overhead and better encoding quality.
log_info "Installing Wayland capture stack (weston, wayvnc, Chromium, websockify)..."
dnf install -y weston wayvnc chromium python3-websockify

# Build dependencies for wf-recorder and ydotool (source builds)
log_info "Installing build dependencies for wf-recorder and ydotool..."
dnf install -y gcc gcc-c++ meson ninja-build cmake scdoc \
    wlroots-devel wayland-devel wayland-protocols-devel ffmpeg-devel \
    libdrm-devel pulseaudio-libs-devel

# Build wf-recorder from source — wlroots screencopy-based screen recorder.
# Not available as an RPM on AlmaLinux 10. Used to capture the compositor's
# output via wlr-screencopy and encode/mux to MPEG-TS multicast.
# Patched to fix colorspace mismatch bug (ammen99/wf-recorder#287): pixman
# delivers frames with AVCOL_SPC_UNSPECIFIED but the filter graph expects
# AVCOL_SPC_RGB. Without the patch, capture stalls after one frame.
if ! command -v wf-recorder &>/dev/null; then
    log_info "Building wf-recorder from source (with colorspace patch)..."
    cd /tmp
    git clone https://github.com/ammen99/wf-recorder.git
    cd wf-recorder
    sed -i '/frame->height = params.height;/a\
    frame->colorspace = AVCOL_SPC_RGB;' src/frame-writer.cpp
    meson setup build --prefix=/usr/local
    ninja -C build
    ninja -C build install
    rm -rf /tmp/wf-recorder
    log_info "wf-recorder installed: $(wf-recorder --version 2>&1 || echo 'built')"
else
    log_info "wf-recorder already installed: $(which wf-recorder)"
fi

# Build ydotool from source — Wayland-native input injection tool.
# Replaces xdotool (X11-only) for sending keyboard events to LibreOffice
# Impress during presentation slideshow control.
if ! command -v ydotool &>/dev/null; then
    log_info "Building ydotool from source..."
    cd /tmp
    git clone https://github.com/ReimuNotMoe/ydotool.git
    cd ydotool
    mkdir build && cd build
    cmake -DCMAKE_INSTALL_PREFIX=/usr/local ..
    make -j"$(nproc)"
    make install
    rm -rf /tmp/ydotool
    log_info "ydotool installed: $(ydotool --version 2>&1 || echo 'built')"
else
    log_info "ydotool already installed: $(which ydotool)"
fi

# Install noVNC static files for the browser-based VNC client.
# websockify serves these files and bridges WebSocket to VNC protocol.
NOVNC_DIR="${APP_DIR}/novnc"
if [[ ! -d "${NOVNC_DIR}/core" ]]; then
    log_info "Installing noVNC static files..."
    cd /tmp
    git clone --depth 1 https://github.com/novnc/noVNC.git novnc-src
    mkdir -p "${NOVNC_DIR}"
    cp -r novnc-src/core novnc-src/vendor novnc-src/vnc_lite.html "${NOVNC_DIR}/"
    rm -rf /tmp/novnc-src
    log_info "noVNC installed to ${NOVNC_DIR}"
else
    log_info "noVNC already installed at ${NOVNC_DIR}"
fi

# Auto-detect the python3 binary — different AL versions install different
# minor versions, and the venv/pip must use the matching binary
PYTHON3_BIN=$(command -v python3)
PYTHON3_VERSION=$("${PYTHON3_BIN}" --version 2>&1 | awk '{print $2}')
log_info "Python: ${PYTHON3_VERSION} at ${PYTHON3_BIN}"

if ! command -v ffmpeg &>/dev/null; then
    log_error "ffmpeg installation failed"
    exit 1
fi
log_info "ffmpeg: $(ffmpeg -version | head -1)"

# Initialize PostgreSQL if not already done. postgresql-setup creates the
# data directory and default configuration.
if [[ ! -f /var/lib/pgsql/data/PG_VERSION ]]; then
    postgresql-setup --initdb
    log_info "PostgreSQL data directory initialized"
fi

# Start and enable PostgreSQL
systemctl enable postgresql
systemctl start postgresql

# Create the mediacaster database and mcs user if they don't exist.
# sudo -u postgres runs commands as the PostgreSQL superuser.
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='mcs'" | grep -q 1; then
    sudo -u postgres psql -c "CREATE USER mcs WITH PASSWORD 'mcs';"
    log_info "Created PostgreSQL user: mcs"
fi
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='mediacaster'" | grep -q 1; then
    sudo -u postgres psql -c "CREATE DATABASE mediacaster OWNER mcs;"
    log_info "Created PostgreSQL database: mediacaster"
fi

# Configure pg_hba.conf for local password authentication.
# By default, PostgreSQL uses 'ident' auth for local connections which
# requires OS username == DB username. We need 'scram-sha-256' (password auth)
# since the mcs service user connects with a password.
PG_HBA="/var/lib/pgsql/data/pg_hba.conf"
if ! grep -q "mcs.*mediacaster.*scram-sha-256" "${PG_HBA}" 2>/dev/null; then
    # Insert our rules before the default local rules so they take priority.
    # Both IPv4 (127.0.0.1) and IPv6 (::1) are needed because "localhost"
    # may resolve to either address depending on the system's DNS config.
    sed -i '/^# IPv4 local connections/a host    mediacaster     mcs             127.0.0.1/32            scram-sha-256' "${PG_HBA}"
    sed -i '/^# IPv6 local connections/a host    mediacaster     mcs             ::1/128                 scram-sha-256' "${PG_HBA}"
    # Also allow local socket connections with password auth
    sed -i '/^# "local" is for Unix domain/a local   mediacaster     mcs                                     scram-sha-256' "${PG_HBA}"
    systemctl restart postgresql
    log_info "Configured PostgreSQL authentication for mcs user"
fi

# ---------------------------------------------------------------------------
# Step 3: Node.js (for building the React frontend)
# ---------------------------------------------------------------------------
log_step "Step 3/10: Installing Node.js ${NODE_MAJOR_VERSION}"

if ! command -v node &>/dev/null; then
    # NodeSource provides up-to-date Node.js packages for RHEL-based distros.
    # The setup script adds the repo; then we install from it.
    curl -fsSL "https://rpm.nodesource.com/setup_${NODE_MAJOR_VERSION}.x" | bash -
    dnf install -y nodejs
fi
log_info "Node: $(node --version)  npm: $(npm --version)"

# ---------------------------------------------------------------------------
# Step 4: Application user and directories
# ---------------------------------------------------------------------------
log_step "Step 4/10: Creating user and directories"

# Create a dedicated system user with no login shell — the app runs as this
# user via systemd, and it should never be used for interactive login
if ! id "${APP_USER}" &>/dev/null; then
    useradd --system --home-dir "${APP_DIR}" --shell /sbin/nologin "${APP_USER}"
    log_info "Created system user: ${APP_USER}"
fi

# audio/video groups allow PulseAudio access for audio capture
usermod -a -G audio,video "${APP_USER}" 2>/dev/null || true

# XDG_RUNTIME_DIR is required by weston for Wayland display sockets.
# System users don't get this automatically (no PAM session), so we
# create it manually. The systemd service also creates it via ExecStartPre.
mkdir -p "/run/user/$(id -u ${APP_USER})"
chown "${APP_USER}:${APP_GROUP}" "/run/user/$(id -u ${APP_USER})"

# Create the data directories — media/uploads/thumbnails hold user content,
# db holds the SQLite database, playlists holds generated ffmpeg concat files
mkdir -p "${APP_DIR}"/{media,uploads,thumbnails,playlists}

# ---------------------------------------------------------------------------
# Step 5: Deploy application files
# ---------------------------------------------------------------------------
log_step "Step 5/10: Deploying application"

cp -r "${SCRIPT_DIR}/backend" "${APP_DIR}/"
cp "${SCRIPT_DIR}/requirements.txt" "${APP_DIR}/"
cp -r "${SCRIPT_DIR}/frontend" "${APP_DIR}/"
# Alembic database migration config and version scripts — required for
# automatic schema creation/upgrade on startup (backend/main.py calls
# alembic.command.upgrade("head") during lifespan initialization)
cp "${SCRIPT_DIR}/alembic.ini" "${APP_DIR}/"
cp -r "${SCRIPT_DIR}/alembic" "${APP_DIR}/"

log_info "Creating Python virtual environment..."
"${PYTHON3_BIN}" -m venv "${APP_DIR}/venv"
"${APP_DIR}/venv/bin/pip" install --upgrade pip
"${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

# Copy the custom vnc_embed.html to the noVNC directory for iframe embedding
if [[ -f "${SCRIPT_DIR}/container/vnc_embed.html" ]]; then
    cp "${SCRIPT_DIR}/container/vnc_embed.html" "${APP_DIR}/novnc/"
    log_info "Installed vnc_embed.html to noVNC directory"
fi

# ---------------------------------------------------------------------------
# Step 6: Build frontend
# ---------------------------------------------------------------------------
log_step "Step 6/10: Building React frontend"

cd "${APP_DIR}/frontend"
# --include=dev ensures devDependencies (Vite, build tooling) are
# installed — they're needed for `npm run build` but not at runtime
npm install --include=dev
npm run build
# Remove node_modules after build — they're not needed at runtime
# (the built static files in dist/ are served by nginx)
rm -rf node_modules
log_info "Frontend build complete"

# ---------------------------------------------------------------------------
# Step 7: Permissions
# ---------------------------------------------------------------------------
log_step "Step 7/10: Setting ownership and permissions"

chown -R "${APP_USER}:${APP_GROUP}" "${APP_DIR}"
chmod -R 755 "${APP_DIR}"
# Data directories need group-write so the app can create files
chmod 775 "${APP_DIR}"/{media,uploads,thumbnails,playlists}

# ---------------------------------------------------------------------------
# Step 8: Systemd + nginx
# ---------------------------------------------------------------------------
log_step "Step 8/10: Configuring services"

cp "${SCRIPT_DIR}/systemd/multicast-streamer.service" /etc/systemd/system/

# Generate a persistent JWT secret so sessions survive service restarts.
# Stored in a systemd override file (not in the main unit, which gets
# overwritten on every deploy). Only generated once — subsequent deploys
# preserve the existing key.
OVERRIDE_DIR="/etc/systemd/system/multicast-streamer.service.d"
OVERRIDE_FILE="${OVERRIDE_DIR}/env.conf"
if [[ ! -f "${OVERRIDE_FILE}" ]]; then
    JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(64))")
    mkdir -p "${OVERRIDE_DIR}"
    cat > "${OVERRIDE_FILE}" << SECRETEOF
[Service]
Environment="MCS_SECRET_KEY=${JWT_SECRET}"
Environment="MCS_DATABASE_URL=postgresql://mcs:mcs@localhost:5432/mediacaster"
SECRETEOF
    chmod 600 "${OVERRIDE_FILE}"
    log_info "Generated environment overrides in ${OVERRIDE_FILE}"
else
    # Ensure DATABASE_URL is present in existing override file
    if ! grep -q "MCS_DATABASE_URL" "${OVERRIDE_FILE}" 2>/dev/null; then
        echo 'Environment="MCS_DATABASE_URL=postgresql://mcs:mcs@localhost:5432/mediacaster"' >> "${OVERRIDE_FILE}"
        log_info "Added DATABASE_URL to existing environment overrides"
    fi
    log_info "Environment overrides already exist — preserving"
fi

systemctl daemon-reload
systemctl enable multicast-streamer

# Generate a self-signed TLS certificate if one doesn't exist.
# This provides HTTPS out of the box — replace with real certs for production.
# The cert is valid for 10 years with a SAN covering the server's IP and hostname.
CERT_FILE="/etc/pki/tls/certs/mediacaster.crt"
KEY_FILE="/etc/pki/tls/private/mediacaster.key"
if [[ ! -f "${CERT_FILE}" ]]; then
    log_info "Generating self-signed TLS certificate..."
    CERT_IP=$(hostname -I | awk '{print $1}')
    CERT_HOSTNAME=$(hostname -f 2>/dev/null || hostname)
    openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
        -keyout "${KEY_FILE}" \
        -out "${CERT_FILE}" \
        -subj "/CN=Mediacaster/O=Mediacaster" \
        -addext "subjectAltName=IP:${CERT_IP},DNS:${CERT_HOSTNAME},DNS:localhost"
    chmod 600 "${KEY_FILE}"
    log_info "Self-signed cert generated for ${CERT_IP} / ${CERT_HOSTNAME}"
else
    log_info "TLS certificate already exists — preserving"
fi

# Remove the default nginx welcome page config — it would conflict with
# our server block (both listen on port 80 with server_name _)
rm -f /etc/nginx/conf.d/default.conf

# Comment out the embedded default server block in nginx.conf (typically
# lines 37-57) that also shadows our config. The sed matches the block
# start ("server {" at indent level) through its closing "}" and prepends
# "#MCS#" to each line, making it idempotent (already-commented lines
# won't be double-commented).
if grep -q '^\s*server\s*{' /etc/nginx/nginx.conf 2>/dev/null; then
    sed -i '/^\s*server\s*{/,/^\s*}/{ /^#MCS#/! s/^/#MCS# / }' /etc/nginx/nginx.conf
    log_info "Commented out default server block in /etc/nginx/nginx.conf"
fi

cp "${SCRIPT_DIR}/nginx/multicast-streamer.conf" /etc/nginx/conf.d/
nginx -t
systemctl enable nginx

# ---------------------------------------------------------------------------
# Step 9: Firewall + SELinux + multicast routing
# ---------------------------------------------------------------------------
log_step "Step 9/10: Firewall, SELinux, and multicast routing"

if systemctl is-active --quiet firewalld; then
    firewall-cmd --permanent --add-service=http
    firewall-cmd --permanent --add-service=https
    # Allow multicast traffic in the 239.0.0.0/8 range (administratively scoped).
    # Without this rule, firewalld drops outbound multicast UDP packets from
    # ffmpeg, and receivers see no data even though ffmpeg reports success.
    firewall-cmd --permanent --add-rich-rule='rule family="ipv4" destination address="239.0.0.0/8" accept'
    # Open noVNC websocket ports (6080-6180) and VNC ports (5950-6050) for
    # browser source preview access. These are the ports websockify and x11vnc
    # listen on inside containers running with --network=host.
    firewall-cmd --permanent --add-port=6080-6180/tcp
    firewall-cmd --permanent --add-port=5950-6050/tcp
    firewall-cmd --reload
    log_info "Firewall rules applied"
else
    log_warn "firewalld not running — skipping"
fi

if command -v getenforce &>/dev/null && [[ "$(getenforce)" != "Disabled" ]]; then
    # httpd_can_network_connect allows nginx to proxy to the uvicorn backend.
    # Without this, SELinux blocks nginx from making outbound TCP connections
    # and all API requests return 502 Bad Gateway.
    setsebool -P httpd_can_network_connect 1

    # semanage requires policycoreutils-python-utils — install if missing
    if ! command -v semanage &>/dev/null; then
        dnf install -y policycoreutils-python-utils
    fi

    # Label the app's data directories so nginx (httpd_t context) can
    # serve uploaded media files and thumbnails directly
    semanage fcontext -a -t httpd_sys_rw_content_t \
        "${APP_DIR}/(media|uploads|thumbnails|db|playlists)(/.*)?" 2>/dev/null || true
    restorecon -Rv "${APP_DIR}/"

    # Register the noVNC websocket port range (6080-6180) as http_port_t
    # so nginx can proxy WebSocket connections to websockify inside containers.
    # The -a flag adds; -m modifies if the range overlaps an existing rule.
    semanage port -a -t http_port_t -p tcp 6080-6180 2>/dev/null || \
        semanage port -m -t http_port_t -p tcp 6080-6180 2>/dev/null || true
    # VNC port range (5950-6050) — labeled as http_port_t so SELinux allows
    # websockify (proxied through nginx) to connect to x11vnc
    semanage port -a -t http_port_t -p tcp 5950-6050 2>/dev/null || \
        semanage port -m -t http_port_t -p tcp 5950-6050 2>/dev/null || true

    log_info "SELinux configured"
fi

# Add a static route for the multicast address range so the kernel knows
# which interface to send multicast packets out on. Without this, multicast
# traffic may go to the loopback interface or be dropped entirely, depending
# on the routing table. We persist it to survive reboots.
# Add multicast route for the current session
if ! ip route show | grep -q "239.0.0.0/8"; then
    PRIMARY_IFACE=$(ip route get 1.1.1.1 | awk '{print $5; exit}')
    if [[ -n "${PRIMARY_IFACE}" ]]; then
        ip route add 239.0.0.0/8 dev "${PRIMARY_IFACE}" 2>/dev/null || true
        log_info "Multicast route added via ${PRIMARY_IFACE}"
    fi
fi

# Persist the multicast route across reboots via a NetworkManager dispatcher
# script. This is more reliable than /etc/sysconfig/network-scripts/ which
# is deprecated on AL9+ and unreliable with NetworkManager.
DISPATCHER="/etc/NetworkManager/dispatcher.d/99-multicast-route"
if [[ ! -f "${DISPATCHER}" ]]; then
    cat > "${DISPATCHER}" << 'DISPATCH'
#!/bin/bash
if [ "$2" = "up" ]; then
    ip route add 239.0.0.0/8 dev "$1" 2>/dev/null || true
fi
DISPATCH
    chmod 755 "${DISPATCHER}"
    log_info "Installed NetworkManager dispatcher for multicast route persistence"
fi

# ---------------------------------------------------------------------------
# Start services
# ---------------------------------------------------------------------------
log_step "Starting services"

systemctl start multicast-streamer
systemctl start nginx
sleep 2

if systemctl is-active --quiet multicast-streamer; then
    log_info "multicast-streamer is running"
else
    log_error "multicast-streamer failed — check: journalctl -u multicast-streamer -n 50"
fi

if systemctl is-active --quiet nginx; then
    log_info "nginx is running"
else
    log_error "nginx failed — check: journalctl -u nginx -n 50"
fi

# ---------------------------------------------------------------------------
# Step 10/10: OS cleanup — remove unnecessary packages and services
# ---------------------------------------------------------------------------
log_step "Step 10/10: OS cleanup — removing unnecessary packages and services"

# Switch to text console — the GNOME desktop is not needed for a headless server
systemctl set-default multi-user.target
systemctl stop gdm.service 2>/dev/null || true
systemctl disable gdm.service 2>/dev/null || true

# Disable unnecessary services
log_info "Disabling unnecessary services..."
SERVICES_TO_DISABLE=(
    cups.service cups.socket cups.path cups-browsed.service
    bluetooth.service
    ModemManager.service
    cockpit.socket cockpit.service
    sssd.service sssd-kcm.socket
    at-spi-dbus-bus.service
    avahi-daemon.service avahi-daemon.socket
    abrt-journal-core.service abrt-oops.service abrt-xorg.service abrtd.service
    pcscd.service pcscd.socket
    gnome-remote-desktop.service
    flatpak-system-helper.service
    evolution-addressbook-factory.service evolution-calendar-factory.service evolution-source-registry.service
    geoclue.service
    power-profiles-daemon.service
    switcheroo-control.service
    bolt.service
    low-memory-monitor.service
    tracker-miner-fs-3.service
    usbguard.service
    iio-sensor-proxy.service
    rtkit-daemon.service
)
for svc in "${SERVICES_TO_DISABLE[@]}"; do
    if systemctl list-unit-files "$svc" &>/dev/null; then
        systemctl stop "$svc" 2>/dev/null || true
        systemctl disable "$svc" 2>/dev/null || true
    fi
done

# Remove GNOME desktop environment
log_info "Removing GNOME desktop environment..."
dnf group remove -y "GNOME" 2>/dev/null || true
dnf group remove -y "Graphical Administration Tools" 2>/dev/null || true

# Remove printing, scanning, bluetooth
log_info "Removing printing, bluetooth, and scanner packages..."
dnf remove -y \
    cups cups-libs cups-filters cups-browsed cups-client cups-ipptool \
    cups-filesystem cups-pk-helper \
    gutenprint* foomatic* ghostscript* hplip* \
    sane-backends* libsane* colord colord-libs \
    bluez bluez-libs bluez-obexd gnome-bluetooth gnome-bluetooth-libs \
    2>/dev/null || true

# Remove ModemManager, cockpit, SSSD, flatpak
log_info "Removing ModemManager, cockpit, SSSD, flatpak..."
dnf remove -y \
    ModemManager ModemManager-glib \
    cockpit cockpit-ws cockpit-bridge cockpit-system \
    sssd sssd-client sssd-common sssd-kcm sssd-nfs-idmap \
    sssd-ad sssd-ipa sssd-krb5 sssd-ldap sssd-proxy \
    flatpak flatpak-libs flatpak-session-helper \
    2>/dev/null || true

# Remove accessibility
log_info "Removing accessibility packages..."
dnf remove -y \
    orca at-spi2-core at-spi2-atk \
    brltty speech-dispatcher speech-dispatcher-espeak-ng espeak-ng \
    2>/dev/null || true

# Remove unnecessary fonts (keep dejavu for basic rendering)
log_info "Removing unnecessary fonts..."
dnf remove -y \
    google-noto-cjk* google-noto-sans-cjk* google-noto-serif-cjk* \
    google-noto-sans-mono-cjk* \
    google-noto-sans-ethiopic* google-noto-sans-lisu* \
    google-noto-sans-math* google-noto-sans-gurmukhi* \
    google-noto-sans-sinhala* google-noto-sans-thai* \
    google-noto-sans-tamil* google-noto-sans-telugu* \
    google-noto-sans-kannada* google-noto-sans-bengali* \
    google-noto-sans-devanagari* google-noto-sans-gujarati* \
    google-noto-sans-malayalam* google-noto-sans-oriya* \
    google-noto-sans-tibetan* google-noto-sans-khmer* \
    google-noto-sans-lao* google-noto-sans-myanmar* \
    google-noto-sans-georgian* google-noto-sans-armenian* \
    google-noto-sans-hebrew* google-noto-sans-arabic* \
    google-noto-emoji* google-noto-color-emoji* \
    jomolhari-fonts sil-padauk-fonts khmer-os-system-fonts \
    lohit-* paktype-* smc-* thai-scalable-* \
    abattis-cantarell-fonts adobe-source-code-pro-fonts \
    2>/dev/null || true

# Remove GNOME apps, Wayland/X11, ABRT, and misc desktop packages
log_info "Removing GNOME applications and desktop components..."
dnf remove -y \
    gnome-shell gnome-session gnome-session-wayland-session \
    gnome-settings-daemon gnome-control-center \
    gnome-terminal gnome-text-editor gnome-calculator \
    gnome-characters gnome-clocks gnome-color-manager \
    gnome-connections gnome-console gnome-contacts \
    gnome-disk-utility gnome-font-viewer gnome-logs \
    gnome-maps gnome-photos gnome-remote-desktop \
    gnome-screenshot gnome-shell-extension-* \
    gnome-software gnome-system-monitor gnome-tour \
    gnome-user-docs gnome-weather gnome-tweaks \
    gnome-boxes gnome-calendar gnome-menus \
    gnome-online-accounts gnome-initial-setup \
    gnome-keyring gnome-classic-session \
    gdm mutter \
    nautilus nautilus-extensions \
    evince evince-libs totem totem-pl-parser \
    eog cheese baobab file-roller loupe \
    gedit rhythmbox shotwell simple-scan \
    yelp yelp-libs yelp-xsl \
    evolution evolution-data-server \
    tracker tracker-miners \
    gjs libgjs \
    xorg-x11-server-Xwayland xwayland-run \
    abrt* libreport* \
    geoclue2 geoclue2-libs bolt switcheroo-control \
    iio-sensor-proxy low-memory-monitor power-profiles-daemon \
    pcscd pcsc-lite pcsc-lite-libs usbguard realmd adcli \
    ibus ibus-gtk3 ibus-gtk4 ibus-libzhuyin ibus-typing-booster \
    libpinyin malcontent malcontent-libs \
    2>/dev/null || true

# NOTE: Build tools (gcc, meson, cmake) are kept installed because they were
# needed to build wf-recorder and ydotool, and may be needed for future rebuilds.

# Protect application dependencies from autoremove, then clean up orphans
log_info "Cleaning up orphaned dependencies..."
dnf mark install postgresql-server postgresql python3 nginx ffmpeg nodejs openssl \
    weston wayvnc chromium python3-websockify \
    gcc gcc-c++ meson ninja-build cmake \
    wlroots-devel wayland-devel ffmpeg-devel 2>/dev/null || true
dnf autoremove -y
dnf clean all

log_info "OS cleanup complete — $(rpm -qa | wc -l) packages remaining"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
SERVER_IP=$(hostname -I | awk '{print $1}')
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Multicast Streamer — Deployment Complete${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  Web UI:        ${CYAN}http://${SERVER_IP}/${NC}"
echo -e "  API Docs:      ${CYAN}http://${SERVER_IP}/docs${NC}"
echo ""
echo -e "  Default login:  ${YELLOW}admin / changeme${NC}"
echo -e "  ${RED}⚠  Change the default password after first login!${NC}"
echo ""
echo -e "  Service management:"
echo -e "    systemctl {start|stop|restart} multicast-streamer"
echo -e "    journalctl -u multicast-streamer -f"
echo ""
echo -e "  Config overrides: MCS_SECRET_KEY, MCS_ADMIN_PASS, MCS_TRANSCODE_RESOLUTION"
echo -e "  Set via: sudo systemctl edit multicast-streamer"
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
