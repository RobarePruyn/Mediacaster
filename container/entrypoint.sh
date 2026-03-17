#!/usr/bin/env bash
# ==========================================================================
# Browser Source Container Entrypoint
# ==========================================================================
# Launches the full capture pipeline inside the container:
#   Xvfb → Firefox (kiosk) → x11vnc → websockify/noVNC → ffmpeg x11grab
#
# All config comes from environment variables set by the browser_manager
# service when it calls `podman run --env ...`. See Containerfile for defaults.
#
# Process lifecycle: all child processes are tracked by PID. If any child
# exits unexpectedly, the cleanup trap tears down all others and the
# container exits, signaling the browser_manager to handle the failure.
# ==========================================================================

set -euo pipefail

# Construct the X11 display identifier (e.g., ":50") from the numeric
# display number. Each browser source container uses a unique DISPLAY_NUM
# to avoid collisions when running with --network=host.
DISPLAY=":${DISPLAY_NUM}"
export DISPLAY

# Track child PIDs for the cleanup trap
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
# Trap container stop signals (SIGTERM from podman stop) and script exit
trap cleanup SIGTERM SIGINT EXIT

# ---- 1. Start Xvfb (virtual X11 framebuffer) ----
# Xvfb creates a virtual display that Firefox renders into and ffmpeg
# captures from. The "x24" suffix sets 24-bit color depth (true color).
# -ac disables access control (no xauth needed within the container).
# -nolisten tcp prevents remote X11 connections (security; VNC handles remote access).
echo "[entrypoint] Starting Xvfb on ${DISPLAY} at ${RESOLUTION}x24"
Xvfb "${DISPLAY}" -screen 0 "${RESOLUTION}x24" -ac -nolisten tcp &
PIDS+=($!)
sleep 1

# Verify Xvfb is running before proceeding — Firefox and ffmpeg both
# depend on the display being available
if ! kill -0 "${PIDS[0]}" 2>/dev/null; then
    echo "[entrypoint] ERROR: Xvfb failed to start"
    exit 1
fi

# ---- 2. Start PulseAudio (if audio capture enabled) ----
# When CAPTURE_AUDIO=true, we create a virtual PulseAudio sink that Firefox
# routes its audio output to. ffmpeg then captures from the sink's "monitor"
# source (the loopback tap of what's being played into the sink).
PULSE_SINK=""
if [[ "${CAPTURE_AUDIO}" == "true" ]]; then
    echo "[entrypoint] Starting PulseAudio for audio capture"
    # --exit-idle-time=-1 prevents PulseAudio from auto-exiting when idle
    pulseaudio --start --exit-idle-time=-1 2>/dev/null || true
    sleep 0.5

    # module-null-sink creates a virtual audio device with no physical output.
    # Firefox sees it as a real audio device and plays into it. The ".monitor"
    # source (used later by ffmpeg) captures everything played to this sink.
    PULSE_SINK="browser_sink"
    pactl load-module module-null-sink \
        sink_name="${PULSE_SINK}" \
        sink_properties=device.description=BrowserAudio 2>/dev/null || true

    export PULSE_SINK
fi

# ---- 3. Start Firefox in kiosk mode ----
echo "[entrypoint] Starting Firefox: ${URL}"

# Create a disposable profile directory so Firefox starts clean every time
# (no session restore dialogs, no cached state from previous runs)
PROFILE_DIR="/tmp/firefox_profile"
mkdir -p "${PROFILE_DIR}"

# user.js overrides are applied at profile load time and take priority
# over prefs.js. This is the only reliable way to suppress all first-run
# UI and disable features that interfere with headless kiosk operation.
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
// Hardware acceleration must be disabled — there's no real GPU in Xvfb,
// and attempting GL calls causes rendering failures or black frames
user_pref("layers.acceleration.disabled", true);
user_pref("gfx.xrender.enabled", false);
// Prevent safe mode prompt after a crash (container restarts look like crashes)
user_pref("toolkit.startup.max_resumed_crashes", -1);
// Force all new windows/popups into the same window and suppress
// fullscreen permission prompts (kiosk pages often go fullscreen)
user_pref("browser.link.open_newwindow", 1);
user_pref("browser.link.open_newwindow.restriction", 0);
user_pref("full-screen-api.warning.timeout", 0);
user_pref("full-screen-api.approval-required", false);
user_pref("dom.disable_window_move_resize", false);
PREFS

# --no-remote prevents Firefox from trying to reuse an existing instance.
# --kiosk renders the page in chromeless fullscreen (no address bar, no tabs).
firefox \
    --no-remote \
    --profile "${PROFILE_DIR}" \
    --kiosk "${URL}" &
PIDS+=($!)
echo "[entrypoint] Firefox PID: ${PIDS[-1]}"

# Give Firefox time to create its window and render the initial page.
# 4 seconds is conservative but necessary — Firefox on Xvfb without GPU
# acceleration can be slow to initialize.
sleep 4

# Force Firefox window to fill the entire virtual display.
# --kiosk mode doesn't always maximize properly without a window manager
# (there's no WM in this container), so we use xdotool to explicitly
# move and resize the window to cover the full framebuffer.
echo "[entrypoint] Forcing Firefox to fill display (${RESOLUTION})"
W=$(echo "${RESOLUTION}" | cut -d'x' -f1)
H=$(echo "${RESOLUTION}" | cut -d'x' -f2)

# Retry up to 3 times — Firefox's window may not be mapped yet
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
# x11vnc attaches to the existing Xvfb display (unlike Xvnc which creates
# its own). This lets operators interact with Firefox (click, type, navigate)
# through a VNC client or the noVNC web interface.
echo "[entrypoint] Starting x11vnc on port ${VNC_PORT}"
# x11vnc flags:
#   -noxdamage: disable X DAMAGE extension — causes visual artifacts in Xvfb
#               where there's no real compositor to handle damage events
#   -cursor arrow: show arrow cursor in VNC (default is dot)
#   -xkb: enable X keyboard extension for proper key mapping
#   -repeat: enable keyboard auto-repeat (disabled by default in x11vnc)
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
# websockify acts as a WebSocket-to-TCP bridge: it accepts WebSocket
# connections from the noVNC HTML5 client on NOVNC_PORT and proxies
# them to the raw VNC protocol on localhost:VNC_PORT.
# --web serves the noVNC static files (HTML/JS/CSS) from /opt/novnc.
echo "[entrypoint] Starting noVNC on port ${NOVNC_PORT}"
websockify \
    --web /opt/novnc \
    "${NOVNC_PORT}" \
    "localhost:${VNC_PORT}" &
PIDS+=($!)

# ---- 6. Start ffmpeg x11grab → multicast output ----
# Build the multicast output URL. pkt_size=1316 is the standard MPEG-TS
# over UDP payload size (7 × 188-byte TS packets = 1316, fits in one
# Ethernet frame with IP/UDP headers under the 1500-byte MTU).
# TTL controls how many router hops multicast packets can traverse.
# Build the multicast output URL with optional interface binding.
# localaddr forces ffmpeg to send from a specific interface IP, preventing
# packets from going to loopback or the wrong NIC. MULTICAST_IFACE_ADDR
# is set by browser_manager.py from the host's primary interface IP.
MULTICAST_URL="udp://${MULTICAST_ADDR}:${MULTICAST_PORT}?pkt_size=1316&ttl=${MULTICAST_TTL}"
if [[ -n "${MULTICAST_IFACE_ADDR:-}" ]]; then
    MULTICAST_URL="${MULTICAST_URL}&localaddr=${MULTICAST_IFACE_ADDR}"
fi

echo "[entrypoint] Starting ffmpeg capture → ${MULTICAST_URL}"

# Build the ffmpeg command as an array for proper argument quoting.
# The pipeline: x11grab input → libx264 encode → AAC audio → MPEG-TS mux → UDP output
FFMPEG_CMD=(
    ffmpeg -y
    # Video input: capture the virtual X11 display at the configured
    # resolution and frame rate
    -f x11grab
    -framerate "${FRAMERATE}"
    -video_size "${RESOLUTION}"
    -i "${DISPLAY}"
)

if [[ "${CAPTURE_AUDIO}" == "true" && -n "${PULSE_SINK}" ]]; then
    # Audio input: capture from the PulseAudio null-sink's monitor source.
    # The ".monitor" suffix is PulseAudio convention — it's a loopback tap
    # of everything being played into the sink by Firefox.
    FFMPEG_CMD+=(
        -f pulse
        -i "${PULSE_SINK}.monitor"
    )
else
    # No browser audio — generate a silent stereo audio track. MPEG-TS
    # receivers expect both audio and video PIDs; a missing audio stream
    # causes player errors or sync issues on many hardware decoders.
    FFMPEG_CMD+=(
        -f lavfi
        -i "anullsrc=r=48000:cl=stereo"
    )
fi

FFMPEG_CMD+=(
    # Video encoding: H.264 Main profile for broad receiver compatibility.
    # "ultrafast" preset minimizes encoding latency at the cost of bitrate
    # efficiency — acceptable for live capture where latency matters more
    # than compression ratio. "zerolatency" tune disables B-frames and
    # lookahead for minimal encode-to-output delay.
    -c:v libx264
    -profile:v main
    -preset ultrafast
    -tune zerolatency
    -b:v "${VIDEO_BITRATE}"
    -maxrate "${VIDEO_BITRATE}"
    # bufsize controls the VBV (video buffering verifier) buffer. Setting it
    # equal to bitrate gives a 1-second buffer — tight enough for low latency
    # but large enough to avoid excessive quality fluctuation.
    -bufsize "$(echo "${VIDEO_BITRATE}" | sed 's/M//')M"
    # yuv420p is required for compatibility — many decoders reject yuv444p
    -pix_fmt yuv420p
    # GOP size = 2× frame rate (e.g., 60 frames at 30fps = keyframe every 2s).
    # Shorter GOPs allow faster channel-change/tune-in at the receiver but
    # reduce compression efficiency.
    -g "$(( ${FRAMERATE} * 2 ))"
    # Audio encoding: AAC at 48kHz stereo — standard for broadcast MPEG-TS
    -c:a aac
    -b:a "${AUDIO_BITRATE}"
    -ac 2
    -ar 48000
    # Output: MPEG-TS container over UDP to the multicast address.
    # mpegts_transport_stream_id identifies this stream in multi-program
    # transport scenarios (not strictly necessary for single-program but
    # good practice).
    -f mpegts
    -mpegts_transport_stream_id 1
    "${MULTICAST_URL}"
)

# Log ffmpeg output to a file for debugging multicast/encoding issues.
# Redirecting directly (not via tee/pipe) so $! captures ffmpeg's actual PID
# for the process monitor loop below. View with: podman exec <name> cat /tmp/ffmpeg.log
"${FFMPEG_CMD[@]}" > /tmp/ffmpeg.log 2>&1 &
PIDS+=($!)
echo "[entrypoint] ffmpeg PID: ${PIDS[-1]}"

echo "[entrypoint] Browser source fully started"
echo "[entrypoint]   Display:   ${DISPLAY}"
echo "[entrypoint]   VNC:       port ${VNC_PORT}"
echo "[entrypoint]   noVNC:     port ${NOVNC_PORT}"
echo "[entrypoint]   Multicast: ${MULTICAST_URL}"

# ---- Wait for any child to exit (indicates a failure) ----
# Poll all tracked PIDs every 2 seconds. If any process has exited
# (e.g., ffmpeg crash, Firefox segfault), trigger cleanup and container exit.
# The browser_manager on the host detects the container stop and can
# auto-restart if configured to do so.
while true; do
    for i in "${!PIDS[@]}"; do
        if ! kill -0 "${PIDS[$i]}" 2>/dev/null; then
            echo "[entrypoint] Process ${PIDS[$i]} exited unexpectedly"
            cleanup
        fi
    done
    sleep 2
done
