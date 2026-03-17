#!/usr/bin/env bash
# ==========================================================================
# Browser Source Container Entrypoint
# Launches the full capture pipeline inside the container:
#   Xvfb → Firefox (kiosk) → x11vnc → websockify/noVNC → ffmpeg x11grab
# All config comes from environment variables set by the browser manager.
# ==========================================================================

set -euo pipefail

DISPLAY=":${DISPLAY_NUM}"
export DISPLAY

# Track child PIDs for cleanup
PIDS=()

cleanup() {
    echo "[entrypoint] Shutting down..."
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null
    echo "[entrypoint] All processes stopped"
    exit 0
}
trap cleanup SIGTERM SIGINT EXIT

# ---- 1. Start Xvfb (virtual X11 framebuffer) ----
echo "[entrypoint] Starting Xvfb on ${DISPLAY} at ${RESOLUTION}x24"
Xvfb "${DISPLAY}" -screen 0 "${RESOLUTION}x24" -ac -nolisten tcp &
PIDS+=($!)
sleep 1

# Verify Xvfb is running
if ! kill -0 "${PIDS[0]}" 2>/dev/null; then
    echo "[entrypoint] ERROR: Xvfb failed to start"
    exit 1
fi

# ---- 2. Start PulseAudio (if audio capture enabled) ----
PULSE_SINK=""
if [[ "${CAPTURE_AUDIO}" == "true" ]]; then
    echo "[entrypoint] Starting PulseAudio for audio capture"
    # Start PulseAudio in the background with a virtual sink
    pulseaudio --start --exit-idle-time=-1 2>/dev/null || true
    sleep 0.5

    PULSE_SINK="browser_sink"
    pactl load-module module-null-sink \
        sink_name="${PULSE_SINK}" \
        sink_properties=device.description=BrowserAudio 2>/dev/null || true

    export PULSE_SINK
fi

# ---- 3. Start Firefox in kiosk mode ----
echo "[entrypoint] Starting Firefox: ${URL}"

# Create a clean profile directory
PROFILE_DIR="/tmp/firefox_profile"
mkdir -p "${PROFILE_DIR}"

# Write prefs for clean kiosk behavior
cat > "${PROFILE_DIR}/user.js" << 'PREFS'
// Disable first-run, updates, telemetry
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("toolkit.telemetry.enabled", false);
user_pref("app.update.enabled", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("browser.aboutConfig.showWarning", false);
user_pref("browser.tabs.warnOnClose", false);
user_pref("browser.sessionstore.resume_from_crash", false);
// Performance: hardware acceleration off in virtual framebuffer
user_pref("layers.acceleration.disabled", true);
user_pref("gfx.xrender.enabled", false);
// Disable safe mode prompt
user_pref("toolkit.startup.max_resumed_crashes", -1);
// Force window size and suppress kiosk/fullscreen prompts
user_pref("browser.link.open_newwindow", 1);
user_pref("browser.link.open_newwindow.restriction", 0);
user_pref("full-screen-api.warning.timeout", 0);
user_pref("full-screen-api.approval-required", false);
user_pref("dom.disable_window_move_resize", false);
PREFS

firefox \
    --no-remote \
    --profile "${PROFILE_DIR}" \
    --kiosk "${URL}" &
PIDS+=($!)
echo "[entrypoint] Firefox PID: ${PIDS[-1]}"

# Give Firefox time to start and render the initial page
sleep 4

# Force Firefox window to fill the entire virtual display
# Kiosk mode doesn't always maximize properly without a window manager
echo "[entrypoint] Forcing Firefox to fill display (${RESOLUTION})"
W=$(echo "${RESOLUTION}" | cut -d'x' -f1)
H=$(echo "${RESOLUTION}" | cut -d'x' -f2)

# Find the Firefox window and resize/reposition it
for attempt in 1 2 3; do
    WINDOW_ID=$(xdotool search --name "" 2>/dev/null | head -1)
    if [[ -n "${WINDOW_ID}" ]]; then
        xdotool windowmove "${WINDOW_ID}" 0 0
        xdotool windowsize "${WINDOW_ID}" "${W}" "${H}"
        xdotool windowfocus "${WINDOW_ID}"
        echo "[entrypoint] Window ${WINDOW_ID} resized to ${W}x${H}"
        break
    fi
    sleep 1
done

# ---- 4. Start x11vnc (VNC server for interactive control) ----
echo "[entrypoint] Starting x11vnc on port ${VNC_PORT}"
x11vnc \
    -display "${DISPLAY}" \
    -rfbport "${VNC_PORT}" \
    -shared \
    -forever \
    -nopw \
    -noxdamage \
    -cursor arrow \
    -xkb \
    -repeat &
PIDS+=($!)
sleep 0.5

# ---- 5. Start websockify/noVNC (web-based VNC client) ----
echo "[entrypoint] Starting noVNC on port ${NOVNC_PORT}"
websockify \
    --web /opt/novnc \
    "${NOVNC_PORT}" \
    "localhost:${VNC_PORT}" &
PIDS+=($!)

# ---- 6. Start ffmpeg x11grab → multicast output ----
MULTICAST_URL="udp://${MULTICAST_ADDR}:${MULTICAST_PORT}?pkt_size=1316&ttl=${MULTICAST_TTL}"

echo "[entrypoint] Starting ffmpeg capture → ${MULTICAST_URL}"

FFMPEG_CMD=(
    ffmpeg -y
    # Video: grab the virtual display
    -f x11grab
    -framerate "${FRAMERATE}"
    -video_size "${RESOLUTION}"
    -i "${DISPLAY}"
)

if [[ "${CAPTURE_AUDIO}" == "true" && -n "${PULSE_SINK}" ]]; then
    # Audio: capture from the PulseAudio monitor source
    FFMPEG_CMD+=(
        -f pulse
        -i "${PULSE_SINK}.monitor"
    )
else
    # Silent audio track
    FFMPEG_CMD+=(
        -f lavfi
        -i "anullsrc=r=48000:cl=stereo"
    )
fi

FFMPEG_CMD+=(
    # Video encoding — ultrafast + zerolatency for live capture
    -c:v libx264
    -profile:v main
    -preset ultrafast
    -tune zerolatency
    -b:v "${VIDEO_BITRATE}"
    -maxrate "${VIDEO_BITRATE}"
    -bufsize "$(echo "${VIDEO_BITRATE}" | sed 's/M//')M"
    -pix_fmt yuv420p
    -g "$(( ${FRAMERATE} * 2 ))"
    # Audio encoding
    -c:a aac
    -b:a "${AUDIO_BITRATE}"
    -ac 2
    -ar 48000
    # Output
    -f mpegts
    -mpegts_transport_stream_id 1
    "${MULTICAST_URL}"
)

"${FFMPEG_CMD[@]}" &
PIDS+=($!)
echo "[entrypoint] ffmpeg PID: ${PIDS[-1]}"

echo "[entrypoint] Browser source fully started"
echo "[entrypoint]   Display:   ${DISPLAY}"
echo "[entrypoint]   VNC:       port ${VNC_PORT}"
echo "[entrypoint]   noVNC:     port ${NOVNC_PORT}"
echo "[entrypoint]   Multicast: ${MULTICAST_URL}"

# ---- Wait for any child to exit (indicates a failure) ----
# If ffmpeg dies, we should exit so the container stops
while true; do
    for i in "${!PIDS[@]}"; do
        if ! kill -0 "${PIDS[$i]}" 2>/dev/null; then
            echo "[entrypoint] Process ${PIDS[$i]} exited unexpectedly"
            cleanup
        fi
    done
    sleep 2
done
