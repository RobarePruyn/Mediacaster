#!/bin/bash
# cleanup-os.sh — Remove unnecessary packages and services from AlmaLinux 10
# for Mediacaster appliance baseline. Run as root.
#
# This script is destructive — review before running. It removes the GNOME
# desktop environment, printing, bluetooth, scanners, accessibility services,
# unnecessary fonts, flatpak, ModemManager, cockpit, SSSD, and build tools.
# After running, the system boots to a text console (multi-user.target).

set -euo pipefail

echo "=== Mediacaster OS Cleanup ==="
echo "This will remove ~100+ packages and disable unnecessary services."
echo "Press Ctrl+C within 10 seconds to abort."
sleep 10

# ---------------------------------------------------------------------------
# 1. Switch to multi-user (text) target before removing GNOME
# ---------------------------------------------------------------------------
echo ">>> Setting default target to multi-user.target..."
systemctl set-default multi-user.target

# Stop GDM now so the desktop session doesn't interfere with removal
systemctl stop gdm.service 2>/dev/null || true
systemctl disable gdm.service 2>/dev/null || true

# ---------------------------------------------------------------------------
# 2. Disable unnecessary services
# ---------------------------------------------------------------------------
echo ">>> Disabling unnecessary services..."

SERVICES_TO_DISABLE=(
    # Printing
    cups.service
    cups.socket
    cups.path
    cups-browsed.service
    # Bluetooth
    bluetooth.service
    # ModemManager (no modems on a server)
    ModemManager.service
    # Cockpit
    cockpit.socket
    cockpit.service
    # SSSD (not using AD/LDAP for local login)
    sssd.service
    sssd-kcm.socket
    # Accessibility
    at-spi-dbus-bus.service
    # Avahi (mDNS — not needed for multicast streaming)
    avahi-daemon.service
    avahi-daemon.socket
    # ABRT (crash reporting)
    abrt-journal-core.service
    abrt-oops.service
    abrt-xorg.service
    abrtd.service
    # Smartcard
    pcscd.service
    pcscd.socket
    # Remote desktop (GNOME)
    gnome-remote-desktop.service
    # Flatpak
    flatpak-system-helper.service
    # Evolution data server (calendar/contacts)
    evolution-addressbook-factory.service
    evolution-calendar-factory.service
    evolution-source-registry.service
    # Geoclue (location services)
    geoclue.service
    # Power profiles (server doesn't need laptop power management)
    power-profiles-daemon.service
    # Switcheroo (GPU switching — not relevant)
    switcheroo-control.service
    # Bolt (Thunderbolt device management)
    bolt.service
    # Low memory monitor
    low-memory-monitor.service
    # Tracker (file indexing)
    tracker-miner-fs-3.service
    # USB guard (not needed)
    usbguard.service
    # iio sensor proxy (accelerometer etc)
    iio-sensor-proxy.service
    # Realtime kit
    rtkit-daemon.service
)

for svc in "${SERVICES_TO_DISABLE[@]}"; do
    if systemctl list-unit-files "$svc" &>/dev/null; then
        systemctl stop "$svc" 2>/dev/null || true
        systemctl disable "$svc" 2>/dev/null || true
        echo "  Disabled: $svc"
    fi
done

# ---------------------------------------------------------------------------
# 3. Remove GNOME desktop environment and related packages
# ---------------------------------------------------------------------------
echo ">>> Removing GNOME desktop environment..."

# Remove the top-level GNOME group (pulls in most desktop packages)
dnf group remove -y "GNOME" 2>/dev/null || true
dnf group remove -y "Graphical Administration Tools" 2>/dev/null || true

# ---------------------------------------------------------------------------
# 4. Remove specific package groups by category
# ---------------------------------------------------------------------------

echo ">>> Removing printing packages..."
dnf remove -y \
    cups cups-libs cups-filters cups-browsed cups-client cups-ipptool \
    cups-filesystem cups-pk-helper \
    gutenprint* foomatic* ghostscript* hplip* \
    sane-backends* libsane* \
    colord colord-libs \
    2>/dev/null || true

echo ">>> Removing bluetooth packages..."
dnf remove -y \
    bluez bluez-libs bluez-obexd \
    gnome-bluetooth gnome-bluetooth-libs \
    2>/dev/null || true

echo ">>> Removing ModemManager..."
dnf remove -y ModemManager ModemManager-glib 2>/dev/null || true

echo ">>> Removing cockpit..."
dnf remove -y cockpit cockpit-ws cockpit-bridge cockpit-system 2>/dev/null || true

echo ">>> Removing SSSD..."
dnf remove -y \
    sssd sssd-client sssd-common sssd-kcm sssd-nfs-idmap \
    sssd-ad sssd-ipa sssd-krb5 sssd-ldap sssd-proxy \
    2>/dev/null || true

echo ">>> Removing flatpak..."
dnf remove -y flatpak flatpak-libs flatpak-session-helper 2>/dev/null || true

echo ">>> Removing accessibility packages..."
dnf remove -y \
    orca at-spi2-core at-spi2-atk \
    brltty speech-dispatcher speech-dispatcher-espeak-ng espeak-ng \
    2>/dev/null || true

echo ">>> Removing unnecessary fonts (keeping dejavu for basic rendering)..."
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

echo ">>> Removing GNOME applications and desktop components..."
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
    2>/dev/null || true

echo ">>> Removing Wayland/X11 display server packages..."
dnf remove -y \
    xorg-x11-server-Xwayland \
    xwayland-run \
    mutter \
    gdm \
    2>/dev/null || true

echo ">>> Removing ABRT crash reporting..."
dnf remove -y abrt* libreport* 2>/dev/null || true

echo ">>> Removing build tools (not needed — all pip deps use wheels)..."
dnf remove -y \
    gcc gcc-c++ cpp make \
    kernel-headers kernel-devel \
    glibc-devel glibc-headers \
    libstdc++-devel \
    binutils \
    2>/dev/null || true

echo ">>> Removing other unnecessary packages..."
dnf remove -y \
    geoclue2 geoclue2-libs \
    bolt \
    switcheroo-control \
    iio-sensor-proxy \
    low-memory-monitor \
    power-profiles-daemon \
    pcscd pcsc-lite pcsc-lite-libs \
    usbguard \
    realmd adcli \
    ibus ibus-gtk3 ibus-gtk4 ibus-libzhuyin ibus-typing-booster \
    libpinyin \
    malcontent malcontent-libs \
    2>/dev/null || true

# ---------------------------------------------------------------------------
# 5. Clean up orphaned dependencies
# ---------------------------------------------------------------------------
echo ">>> Removing orphaned dependencies..."
dnf autoremove -y

# ---------------------------------------------------------------------------
# 6. Clean dnf cache
# ---------------------------------------------------------------------------
echo ">>> Cleaning dnf cache..."
dnf clean all

# ---------------------------------------------------------------------------
# 7. Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== Cleanup Complete ==="
echo "Default target: $(systemctl get-default)"
echo "Remaining packages: $(rpm -qa | wc -l)"
echo ""
echo "Reboot recommended: sudo reboot"
echo "After reboot, the system will boot to a text console."
