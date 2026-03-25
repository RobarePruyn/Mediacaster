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

# ---- 1b. Start openbox (lightweight window manager) ----
# Without a WM, Firefox's --kiosk mode can't properly maximize — X11 has no
# concept of "maximized" without a WM to enforce it. openbox is the lightest
# WM available on AL9 and handles maximize/fullscreen hints from Firefox.
#
# The rc.xml config tells openbox to maximize all new windows by default,
# remove all decorations (title bar, borders), and apply these rules to
# every window class. This ensures Firefox fills the entire Xvfb framebuffer.
echo "[entrypoint] Starting openbox window manager"
mkdir -p /home/browseruser/.config/openbox
cat > /home/browseruser/.config/openbox/rc.xml << 'OBXML'
<?xml version="1.0" encoding="UTF-8"?>
<openbox_config xmlns="http://openbox.org/3.4/rc">
  <applications>
    <!-- Force all windows to open maximized with no decorations -->
    <application class="*">
      <maximized>yes</maximized>
      <decor>no</decor>
    </application>
  </applications>
  <!-- Disable all desktop features — this is a kiosk, not a desktop -->
  <desktops><number>1</number></desktops>
  <theme><name>Clearlooks</name></theme>
</openbox_config>
OBXML
openbox &
PIDS+=($!)
sleep 0.5

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
// Force H.264 for web video playback. VP9 and AV1 are far more expensive to
// software-decode and will starve the x264 encoder of CPU. H.264 decodes
// roughly 3x faster in software. YouTube and most sites will fall back to
// H.264 when VP9/AV1 are disabled.
user_pref("media.mediasource.vp9.enabled", false);
user_pref("media.av1.enabled", false);
// Reduce Firefox compositor overhead — async panning/zooming and smooth
// scrolling add extra compositing passes that waste CPU in a headless kiosk
user_pref("apz.allow_zooming", false);
user_pref("general.smoothScroll", false);
// Lower content process count — each process has rendering overhead.
// In kiosk mode there's only one tab, so more processes just waste memory and CPU.
user_pref("dom.ipc.processCount", 1);
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

# Append resolution-dependent prefs (can't use variables in a quoted heredoc).
# These set Firefox's initial window geometry to match the Xvfb framebuffer
# so it starts at the right size before openbox maximizes it.
W=$(echo "${RESOLUTION}" | cut -d'x' -f1)
H=$(echo "${RESOLUTION}" | cut -d'x' -f2)
cat >> "${PROFILE_DIR}/user.js" << GEOM
// Initial window geometry — match the Xvfb framebuffer resolution
user_pref("browser.window.width", ${W});
user_pref("browser.window.height", ${H});
GEOM

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

# Ensure Firefox fills the entire virtual display.
# openbox's rc.xml config above forces all windows to maximize, but we
# also use xdotool as a belt-and-suspenders measure to explicitly resize
# and re-maximize the Firefox window after it's had time to render.
echo "[entrypoint] Confirming Firefox fills display (${RESOLUTION})"
W=$(echo "${RESOLUTION}" | cut -d'x' -f1)
H=$(echo "${RESOLUTION}" | cut -d'x' -f2)

# Retry up to 5 times — Firefox may take a while to create its window,
# especially on first launch without GPU acceleration.
for attempt in 1 2 3 4 5; do
    # Search by WM_CLASS (more reliable than window name, which changes
    # with every page navigation). Firefox's WM_CLASS is always "Navigator".
    # || true prevents set -e + pipefail from killing the script when
    # xdotool returns non-zero (no windows found yet)
    WINDOW_ID=$(xdotool search --class Navigator 2>/dev/null | head -1 || true)
    if [[ -n "${WINDOW_ID}" ]]; then
        # Move to origin, resize to full framebuffer, then tell openbox
        # to maximize (handles any WM chrome/offset openbox might add)
        xdotool windowmove "${WINDOW_ID}" 0 0
        xdotool windowsize "${WINDOW_ID}" "${W}" "${H}"
        xdotool windowactivate "${WINDOW_ID}"
        # wmctrl-style maximize via xdotool key (openbox listens to these)
        xdotool key super+Up 2>/dev/null || true
        echo "[entrypoint] Window ${WINDOW_ID} maximized to ${W}x${H}"
        break
    fi
    echo "[entrypoint] Waiting for Firefox window (attempt ${attempt}/5)..."
    sleep 2
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

# Calculate VBV buffer size at 1x the video bitrate. A smaller buffer forces
# the encoder to react faster to scene complexity changes, preventing the
# slow ramp-up that causes macroblocking during transitions. For live screen
# capture, fast reaction is more important than buffer headroom.
BITRATE_NUM="${VIDEO_BITRATE%%[^0-9]*}"
BUFSIZE="${BITRATE_NUM}M"

# Build the ffmpeg command as an array for proper argument quoting.
# The pipeline: x11grab input → libx264 encode → AAC audio → MPEG-TS mux → UDP output
FFMPEG_CMD=(
    ffmpeg -y
    # Video input: capture the virtual X11 display at the configured
    # resolution and frame rate.
    # thread_queue_size: buffer up to 512 frames from x11grab so that
    # momentary encoder stalls don't cause frame drops. The default (8)
    # is far too small for live capture where frame delivery is bursty.
    -thread_queue_size 512
    -f x11grab
    -framerate "${FRAMERATE}"
    -video_size "${RESOLUTION}"
    -i "${DISPLAY}"
)

if [[ "${CAPTURE_AUDIO}" == "true" && -n "${PULSE_SINK}" ]]; then
    # Audio input: capture from the PulseAudio null-sink's monitor source.
    # The ".monitor" suffix is PulseAudio convention — it's a loopback tap
    # of everything being played into the sink by Firefox.
    # thread_queue_size matches the video input to prevent audio drops.
    FFMPEG_CMD+=(
        -thread_queue_size 512
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
    # Force constant frame rate output. x11grab delivers frames at irregular
    # intervals (Xvfb has no vsync), so without CFR enforcement the MPEG-TS
    # output has variable frame timing that receivers display as choppy video.
    # cfr duplicates or drops frames as needed to maintain steady output.
    -vsync cfr
    -r "${FRAMERATE}"
    # Video encoding: H.264 Main profile for broad receiver compatibility.
    # Preset and tune are configurable via the browser source settings in the
    # admin UI. "faster" preset with no tune gives a good balance of quality
    # and latency for live capture. Slower presets improve quality per bit but
    # use more CPU; "zerolatency" tune reduces latency but disables B-frames.
    -c:v libx264
    -profile:v main
    -preset "${ENCODER_PRESET:-faster}"
)

# Conditionally add the -tune flag only if ENCODER_TUNE is set and non-empty.
# When empty, omitting -tune lets libx264 use its default behavior for the
# chosen preset (B-frames enabled, lookahead active = better quality).
if [[ -n "${ENCODER_TUNE:-}" ]]; then
    FFMPEG_CMD+=(-tune "${ENCODER_TUNE}")
fi

FFMPEG_CMD+=(
    -b:v "${VIDEO_BITRATE}"
    -minrate "${VIDEO_BITRATE}"
    -maxrate "${VIDEO_BITRATE}"
    # bufsize at 1x bitrate forces tight rate control — the encoder must
    # maintain target bitrate consistently rather than slowly ramping up.
    # This prevents the "undershoot on static, overshoot on complex" pattern
    # that causes visible macroblocking during page transitions and scrolling.
    -bufsize "${BUFSIZE}"
    # CBR NAL HRD mode pads the stream to maintain constant bitrate even
    # during static scenes, keeping the VBV buffer full so the encoder has
    # immediate headroom when complexity spikes.
    -nal-hrd cbr
    # yuv420p is required for compatibility — many decoders reject yuv444p
    -pix_fmt yuv420p
    # GOP size = frame rate → keyframe every 1 second for fast IPTV channel tune-in.
    # Receivers must decode from a keyframe, so 1-second GOPs minimize channel-change
    # latency at the cost of slightly higher bitrate (more I-frames).
    -g "${FRAMERATE}"
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
