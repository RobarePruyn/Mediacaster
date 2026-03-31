"""
Capture Source Manager — Native Wayland pipeline for multicast streaming.

Replaces the Podman container-based approach (browser_manager.py) with native
Wayland processes running directly on the host. This eliminates container
overhead that contributed to CPU contention and encoding quality degradation
(macroblocking) in browser/presentation source capture.

Manages two source types that share the same capture pipeline:

  BROWSER:      Firefox kiosk mode renders a web URL
  PRESENTATION: LibreOffice Impress renders a slideshow natively

Each source runs as an isolated set of native processes:
    - cage (wlroots kiosk compositor running the app fullscreen)
    - Firefox or LibreOffice Impress (launched by cage as its client)
    - wf-recorder (wlroots screencopy → raw video pipe)
    - ffmpeg (encode + mux → MPEG-TS UDP multicast)
    - wayvnc (VNC server for interactive preview)
    - websockify (WebSocket bridge for noVNC browser-based preview)
    - ydotoold (input injection daemon, presentation sources only)

Process isolation is achieved through per-stream XDG_RUNTIME_DIR directories
and unique Wayland display sockets. Each stream gets its own cage compositor
instance, preventing cross-stream interference.

Port allocation scheme (unchanged from container-based approach):
    Each stream gets a unique display number (starting at 50), which determines
    its VNC port (5950 + display) and noVNC/websockify port (6080 + display).
    These ranges must be open in the firewall (see deploy.sh).
"""

import asyncio
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess
from typing import Dict, Optional
from sqlalchemy.orm import Session
from backend import config
from backend.models import BrowserSource, Stream, StreamStatus, StreamSourceType, Presentation
from backend.services.encoding_profiles import get_effective_bitrate, get_effective_gop_size

logger = logging.getLogger("wayland_manager")

# Port allocation bases — each stream offsets from these by its display number
# Display :50 -> VNC 6000, noVNC 6130; Display :51 -> VNC 6001, noVNC 6131; etc.
DISPLAY_BASE = 50
VNC_PORT_BASE = 5950
NOVNC_PORT_BASE = 6080


def _detect_host_multicast_ip() -> str:
    """Detect the IP address of the host's primary network interface.

    Used to set localaddr= in ffmpeg's UDP multicast output URL, ensuring
    packets are sent from the correct NIC rather than relying on the kernel's
    default routing table (which may route to loopback for multicast).

    Returns empty string if detection fails.
    """
    ip_cmd = "/usr/sbin/ip"
    if not os.path.exists(ip_cmd):
        ip_cmd = "ip"

    try:
        result = subprocess.run(
            [ip_cmd, "route", "get", "1.1.1.1"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            parts = result.stdout.split()
            if "src" in parts:
                return parts[parts.index("src") + 1]
        logger.warning("ip route get returned code %d: %s", result.returncode, result.stderr)
    except (subprocess.TimeoutExpired, OSError, IndexError, ValueError) as exc:
        logger.warning("Failed to detect host multicast interface IP: %s", exc)
    return ""


class ManagedSource:
    """Tracks a running capture source's processes and port assignments.

    Each source has multiple native processes that need coordinated lifecycle
    management. The display number determines all port assignments (VNC, noVNC).
    """

    def __init__(self, stream_id: int, display_number: int):
        self.stream_id = stream_id
        self.display_number = display_number
        # Per-stream isolated runtime directory for Wayland sockets
        self.runtime_dir: str = ""
        # cage uses wl_display_add_socket_auto() which creates wayland-0 in the
        # isolated XDG_RUNTIME_DIR. All child processes use this socket name.
        self.wayland_display: str = "wayland-0"

        # Native process handles (asyncio.subprocess.Process)
        self.weston_proc: Optional[asyncio.subprocess.Process] = None
        self.app_proc: Optional[asyncio.subprocess.Process] = None
        self.wf_recorder_proc: Optional[asyncio.subprocess.Process] = None
        self.ffmpeg_proc: Optional[asyncio.subprocess.Process] = None
        self.wayvnc_proc: Optional[asyncio.subprocess.Process] = None
        self.websockify_proc: Optional[asyncio.subprocess.Process] = None
        self.ydotoold_proc: Optional[asyncio.subprocess.Process] = None

        # X11 display number assigned by XWayland (for presentation sources)
        self.x_display: Optional[int] = None

        # Set to True when stop is called to prevent watcher from flagging error
        self.should_stop = False

    @property
    def vnc_port(self):
        """VNC port for wayvnc connections."""
        return VNC_PORT_BASE + self.display_number

    @property
    def novnc_port(self):
        """Websockify port for noVNC browser-based VNC access."""
        return NOVNC_PORT_BASE + self.display_number

    @property
    def all_procs(self) -> list:
        """All tracked process handles for lifecycle management."""
        return [p for p in [
            self.weston_proc, self.app_proc, self.wf_recorder_proc,
            self.ffmpeg_proc, self.wayvnc_proc, self.websockify_proc,
            self.ydotoold_proc,
        ] if p is not None]


async def _drain_stderr(proc: asyncio.subprocess.Process, label: str):
    """Read and discard stderr from a running process to prevent pipe buffer fill.

    When a process has stderr=PIPE but we only need stderr on failure, this
    background task prevents the pipe buffer from filling up and blocking
    the process during normal operation.
    """
    try:
        while proc.returncode is None:
            data = await proc.stderr.read(4096)
            if not data:
                break
    except (asyncio.CancelledError, OSError, ValueError):
        pass


class WaylandManager:
    """Manages native Wayland process groups for capture source streaming.

    Handles process lifecycle (start, stop, crash detection), display number
    allocation to prevent port conflicts, and DB synchronization of port
    assignments. Drop-in replacement for BrowserManager with identical public API.
    """

    def __init__(self, db_session_factory):
        # Maps stream_id -> ManagedSource for all running capture sources
        self._active: Dict[int, ManagedSource] = {}
        self._db_factory = db_session_factory
        # Tracks which display numbers are in use to prevent port collisions
        self._used_displays = set()

    def _allocate_display(self) -> int:
        """Allocate the next available display number.

        Starts at DISPLAY_BASE (50) and increments until finding one not in use.
        This determines the VNC and noVNC ports for the source.
        """
        display = DISPLAY_BASE
        while display in self._used_displays:
            display += 1
        self._used_displays.add(display)
        return display

    def _release_display(self, display: int):
        """Return a display number to the available pool when a source stops."""
        self._used_displays.discard(display)

    def is_active(self, stream_id: int) -> bool:
        """Check if a capture source is running for the given stream."""
        return stream_id in self._active

    def get_novnc_port(self, stream_id: int) -> Optional[int]:
        """Get the noVNC websockify port for a running capture source.

        Used by the streams API to tell the frontend which port to connect
        the noVNC iframe to for live preview/interaction.
        """
        managed = self._active.get(stream_id)
        return managed.novnc_port if managed else None

    def _create_runtime_dir(self, display_num: int) -> str:
        """Create an isolated XDG_RUNTIME_DIR for a compositor instance.

        Each cage compositor needs its own XDG_RUNTIME_DIR to create a
        unique Wayland socket without colliding with other instances. The
        directory is created under the mcs user's runtime dir.
        """
        # Get the mcs user's UID for the runtime dir path
        uid = os.getuid()
        base_runtime = f"/run/user/{uid}"
        runtime_dir = os.path.join(base_runtime, f"mcs-{display_num}")
        os.makedirs(runtime_dir, exist_ok=True)
        return runtime_dir

    def _cleanup_runtime_dir(self, runtime_dir: str):
        """Remove the per-stream runtime directory after shutdown."""
        try:
            if runtime_dir and os.path.isdir(runtime_dir):
                shutil.rmtree(runtime_dir, ignore_errors=True)
        except OSError as exc:
            logger.debug("Failed to clean runtime dir %s: %s", runtime_dir, exc)

    def _base_env(self, managed: ManagedSource) -> dict:
        """Build the base environment for all child processes of a source.

        All processes need the same XDG_RUNTIME_DIR and WAYLAND_DISPLAY to
        connect to the correct cage compositor instance.
        """
        env = os.environ.copy()
        env["XDG_RUNTIME_DIR"] = managed.runtime_dir
        env["WAYLAND_DISPLAY"] = managed.wayland_display
        # Ensure cage and other tools can find their libraries
        env["HOME"] = os.path.expanduser("~")
        return env

    async def _start_cage(self, managed: ManagedSource, resolution: str,
                          app_cmd: list, app_env: dict = None) -> bool:
        """Launch a headless cage compositor with the given application.

        cage is a wlroots-based kiosk compositor that runs a single application
        fullscreen. Using cage instead of weston because wf-recorder requires the
        wlr-screencopy-unstable-v1 protocol, which is a wlroots extension that
        weston does not implement.

        The application command is passed directly to cage — cage starts the
        compositor and launches the app as one unit.

        Returns True if cage started and the Wayland socket appeared.
        """
        env = self._base_env(managed)
        # Tell wlroots to use the headless backend (no physical display needed)
        env["WLR_BACKENDS"] = "headless"
        # Create exactly one virtual output at the stream's configured resolution
        env["WLR_HEADLESS_OUTPUTS"] = "1"
        width, height = resolution.split("x")
        env["WLR_HEADLESS_RESOLUTION"] = f"{width}x{height}"
        # Force pixman (software) renderer — no GPU/DRM render node on headless servers
        env["WLR_RENDERER"] = "pixman"
        # Merge any application-specific env vars (e.g., MOZ_ENABLE_WAYLAND for Firefox)
        if app_env:
            env.update(app_env)

        cage_cmd = [
            config.CAGE_PATH,
            "--",
        ] + app_cmd

        # Snapshot existing X11 sockets before cage starts so we can detect
        # the new XWayland display created by this cage instance.
        pre_x_sockets = self._snapshot_x_sockets()

        logger.info("Starting cage: %s (runtime=%s)", " ".join(cage_cmd), managed.runtime_dir)
        managed.weston_proc = await asyncio.create_subprocess_exec(
            *cage_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )

        # Wait for the Wayland socket to appear (indicates cage is ready)
        socket_path = os.path.join(managed.runtime_dir, managed.wayland_display)
        for attempt in range(30):
            await asyncio.sleep(0.25)
            if os.path.exists(socket_path):
                logger.info("Cage ready: socket=%s pid=%d", socket_path, managed.weston_proc.pid)
                # Detect XWayland display number for X11 key injection.
                # XWayland is reparented to PID 1, so we detect by comparing
                # X sockets before/after cage start.
                managed.x_display = self._detect_x_display(pre_x_sockets)
                if managed.x_display is not None:
                    logger.info("XWayland display detected: :%d", managed.x_display)
                else:
                    # Store snapshot for post-startup detection
                    managed._pre_x_sockets = pre_x_sockets
                return True
            # Check if cage exited prematurely
            if managed.weston_proc.returncode is not None:
                try:
                    stderr = await asyncio.wait_for(managed.weston_proc.stderr.read(), timeout=3)
                    logger.error("Cage exited prematurely (rc=%d): %s",
                                 managed.weston_proc.returncode, stderr.decode()[:500])
                except asyncio.TimeoutError:
                    logger.error("Cage exited prematurely (rc=%d), stderr read timed out",
                                 managed.weston_proc.returncode)
                return False

        logger.error("Timed out waiting for cage socket at %s", socket_path)
        return False

    @staticmethod
    def _snapshot_x_sockets() -> set:
        """Return the set of X11 socket numbers currently in /tmp/.X11-unix/."""
        x11_dir = "/tmp/.X11-unix"
        result = set()
        try:
            for name in os.listdir(x11_dir):
                if name.startswith("X") and name[1:].isdigit():
                    result.add(int(name[1:]))
        except OSError:
            pass
        return result

    def _detect_x_display(self, pre_sockets: set) -> Optional[int]:
        """Find a new XWayland display by comparing against a pre-start snapshot.

        XWayland is reparented to PID 1 by systemd, so we can't rely on
        parent PID tracking. Instead, compare the current X sockets in
        /tmp/.X11-unix/ against the snapshot taken before cage started.
        """
        current = self._snapshot_x_sockets()
        new_sockets = current - pre_sockets
        if len(new_sockets) == 1:
            return new_sockets.pop()
        elif len(new_sockets) > 1:
            # Multiple new sockets — return the highest (most recently created)
            return max(new_sockets)
        return None

    def _build_firefox_cmd(self, managed: ManagedSource, url: str,
                           resolution: str) -> tuple:
        """Build Firefox command and env for kiosk mode.

        Creates a disposable profile with optimized settings for headless
        kiosk operation (no telemetry, no GPU accel, H.264 decode preference).

        Returns (cmd_list, env_dict) for use with _start_cage().
        """
        width, height = resolution.split("x")
        env = {
            # Force Wayland backend — Firefox defaults to X11 if both are available
            "MOZ_ENABLE_WAYLAND": "1",
            # Disable GPU compositing — no real GPU in headless compositor
            "MOZ_WEBRENDER": "0",
        }

        # Create a disposable profile directory
        profile_dir = os.path.join(managed.runtime_dir, "firefox_profile")
        os.makedirs(profile_dir, exist_ok=True)

        # Write user.js preferences for kiosk operation
        user_js = os.path.join(profile_dir, "user.js")
        with open(user_js, "w") as f:
            f.write("""\
// Disable first-run, updates, telemetry
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("toolkit.telemetry.enabled", false);
user_pref("app.update.enabled", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("browser.aboutConfig.showWarning", false);
user_pref("browser.tabs.warnOnClose", false);
user_pref("browser.sessionstore.resume_from_crash", false);
// Disable hardware acceleration — no real GPU in headless compositor
user_pref("layers.acceleration.disabled", true);
user_pref("gfx.xrender.enabled", false);
// Force H.264 for web video playback — VP9/AV1 are too CPU-expensive
user_pref("media.mediasource.vp9.enabled", false);
user_pref("media.av1.enabled", false);
// Reduce compositor overhead
user_pref("apz.allow_zooming", false);
user_pref("general.smoothScroll", false);
// Single content process for kiosk mode
user_pref("dom.ipc.processCount", 1);
// Prevent safe mode prompt after crash
user_pref("toolkit.startup.max_resumed_crashes", -1);
// Force single window, suppress fullscreen prompts
user_pref("browser.link.open_newwindow", 1);
user_pref("browser.link.open_newwindow.restriction", 0);
user_pref("full-screen-api.warning.timeout", 0);
user_pref("full-screen-api.approval-required", false);
user_pref("dom.disable_window_move_resize", false);
""")
            # Resolution-dependent window geometry prefs
            f.write(f'user_pref("browser.window.width", {width});\n')
            f.write(f'user_pref("browser.window.height", {height});\n')

        cmd = [
            config.FIREFOX_PATH,
            "--no-remote",
            "--profile", profile_dir,
            "--kiosk", url,
        ]

        logger.info("Firefox command prepared: %s", url)
        return cmd, env

    def _build_libreoffice_cmd(self, managed: ManagedSource,
                               file_path: str, resolution: str) -> tuple:
        """Build LibreOffice Impress command and env for slideshow mode.

        LibreOffice's --show flag opens directly in presentation/slideshow mode.
        The slideshow fills the compositor output and responds to keyboard
        navigation (Right/Left/Space/Escape) via ydotool.

        Returns (cmd_list, env_dict) for use with _start_cage().
        """
        env = {
            # Use the generic X11 VCL plugin connecting through XWayland.
            # The TDF LibreOffice build bundles its own GTK libraries which may
            # lack Wayland support ("no suitable windowing system found").
            # cage's XWayland integration provides a DISPLAY that soffice.bin
            # can connect to via the standard X11 path.
            "SAL_USE_VCLPLUGIN": "gen",
            # Disable glamor (GPU-accelerated rendering) in XWayland.
            # On headless servers without a GPU, XWayland's glamor can't get
            # proper GBM interfaces, causing a fatal error (rc=81).
            # This forces XWayland to use software acceleration instead.
            "XWAYLAND_NO_GLAMOR": "1",
        }

        # Use the soffice wrapper script (not soffice.bin directly).
        # The wrapper sets up LD_LIBRARY_PATH and other env vars that soffice.bin
        # needs. Now that cage provides a working XWayland display (DISPLAY=:0)
        # via XWAYLAND_NO_GLAMOR, the oosplash launcher can connect to it.
        soffice_cmd = config.LIBREOFFICE_PATH

        # Remove stale lock files from crashed previous instances.
        # LibreOffice creates .~lock.<filename># in the same directory as the
        # document. If a previous cage/LO process was killed, the lock file
        # persists and LO shows a "locked for editing" dialog instead of the
        # slideshow — unusable in a headless kiosk environment.
        lock_file = os.path.join(
            os.path.dirname(file_path),
            ".~lock." + os.path.basename(file_path) + "#"
        )
        if os.path.exists(lock_file):
            logger.info("Removing stale LibreOffice lock file: %s", lock_file)
            try:
                os.remove(lock_file)
            except OSError as e:
                logger.warning("Could not remove lock file %s: %s", lock_file, e)

        # Create a LibreOffice user profile that skips the first-run wizard.
        # The --nofirststartwizard flag doesn't suppress it in newer TDF builds;
        # the wizard checks for a registrymodifications.xcu file in the user profile.
        lo_profile = os.path.join(managed.runtime_dir, "lo_profile")
        lo_user_dir = os.path.join(lo_profile, "user")
        os.makedirs(lo_user_dir, exist_ok=True)
        # Write a minimal registrymodifications.xcu that marks first-run as complete
        reg_file = os.path.join(lo_user_dir, "registrymodifications.xcu")
        with open(reg_file, "w") as f:
            f.write("""<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry"
           xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Setup/Product">
  <prop oor:name="ooSetupLastVersion" oor:op="fuse">
    <value>26.2</value>
  </prop>
</item>
<item oor:path="/org.openoffice.Office.Common/Misc">
  <prop oor:name="ShowTipOfTheDay" oor:op="fuse">
    <value>false</value>
  </prop>
</item>
</oor:items>
""")

        # Wrap LO in a restart loop so the slideshow never kills the pipeline.
        # When the user presses Next past the last slide, LO exits --show mode
        # and terminates. Without this wrapper, cage would lose its child process
        # and exit too, tearing down the entire capture pipeline (502 error).
        # The loop restarts LO from slide 1 after a brief pause.
        # Lock file cleanup is inside the loop so it runs before each restart.
        wrapper_script = os.path.join(managed.runtime_dir, "lo_loop.sh")
        with open(wrapper_script, "w") as f:
            f.write("#!/bin/sh\n")
            f.write("while true; do\n")
            f.write(f'  rm -f "{lock_file}"\n')
            f.write(f'  "{soffice_cmd}" --norestore --nofirststartwizard '
                    f'"-env:UserInstallation=file://{lo_profile}" '
                    f'--show "{file_path}"\n')
            f.write("  sleep 1\n")
            f.write("done\n")
        os.chmod(wrapper_script, 0o755)

        cmd = ["/bin/sh", wrapper_script]

        logger.info("LibreOffice command prepared: %s (binary: %s, looping wrapper: %s)",
                     file_path, soffice_cmd, wrapper_script)
        return cmd, env

    async def _start_audio(self, managed: ManagedSource) -> Optional[str]:
        """Set up PulseAudio virtual sink for audio capture.

        Creates a null-sink that the application (Firefox/LibreOffice) routes
        audio to. ffmpeg captures from the sink's monitor source. Uses
        pipewire-pulseaudio compatibility layer on AL10.

        Returns the PulseAudio sink name, or None if audio setup fails.
        """
        env = self._base_env(managed)
        sink_name = f"mcs_sink_{managed.display_number}"

        # Start PulseAudio server for this runtime context
        try:
            proc = await asyncio.create_subprocess_exec(
                "pulseaudio", "--start", "--exit-idle-time=-1",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
            await proc.wait()
            await asyncio.sleep(0.5)
        except (OSError, FileNotFoundError):
            logger.warning("PulseAudio not available — audio capture disabled")
            return None

        # Create a virtual sink with no physical output
        try:
            proc = await asyncio.create_subprocess_exec(
                "pactl", "load-module", "module-null-sink",
                f"sink_name={sink_name}",
                "sink_properties=device.description=MCSAudio",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
            await proc.wait()
            logger.info("PulseAudio sink created: %s", sink_name)
            return sink_name
        except (OSError, FileNotFoundError):
            logger.warning("pactl not available — audio capture disabled")
            return None

    async def _start_capture_pipeline(self, managed: ManagedSource,
                                       stream: Stream,
                                       multicast_address: str,
                                       multicast_port: int,
                                       capture_audio: bool) -> bool:
        """Start the wf-recorder → ffmpeg capture and encoding pipeline.

        wf-recorder captures the cage compositor's output via the wlroots
        screencopy protocol, encodes to H.264/H.265, and outputs MPEG-TS
        to a pipe. ffmpeg reads the encoded video, adds a silent audio
        track (or PulseAudio capture), and outputs the final MPEG-TS
        multicast stream with video passthrough (-c:v copy).

        Encoding is done in wf-recorder rather than ffmpeg because
        wf-recorder's rawvideo pipe output is broken with the pixman
        renderer's native pixel format (gbrp/0bgr) — the screencopy
        loop stalls after one frame. Using wf-recorder's internal
        encoder avoids this code path entirely.
        """
        resolution = stream.resolution or "1920x1080"
        codec = stream.codec or "h264"
        framerate = stream.framerate or 30
        bitrate = get_effective_bitrate(resolution, framerate, codec, stream.video_bitrate)
        gop_size = get_effective_gop_size(framerate, stream.gop_size)
        width, height = resolution.split("x")

        # Build the multicast output URL
        host_multicast_ip = _detect_host_multicast_ip()
        multicast_url = (
            f"udp://{multicast_address}:{multicast_port}"
            f"?pkt_size=1316&ttl={config.MULTICAST_TTL}"
        )
        if host_multicast_ip:
            multicast_url += f"&localaddr={host_multicast_ip}"
            logger.info("Multicast interface binding: localaddr=%s", host_multicast_ip)

        env = self._base_env(managed)

        # --- Build wf-recorder command ---
        # wf-recorder captures cage's display via wlr-screencopy-unstable-v1.
        # --no-dmabuf forces SHM buffers since the pixman renderer can't do DMA-BUF.
        #
        # wf-recorder handles capture, encoding, AND output directly to the
        # multicast URL. Previous attempts piping rawvideo or MPEG-TS through
        # a pipe to ffmpeg all failed — wf-recorder receives one screencopy
        # frame then stops, writing zero bytes to the pipe. By having
        # wf-recorder write directly to the UDP multicast URL (no pipe),
        # we bypass whatever output buffering issue causes the stall.
        #
        # Audio is currently not muxed (video-only MPEG-TS). Future: add
        # audio via a separate ffmpeg process reading from the multicast
        # stream, or via PulseAudio capture when wf-recorder supports it.
        capture_res = "1280x720"  # cage headless default (wlroots 0.18)
        need_scale = (capture_res != resolution)

        # wf-recorder's -p flag passes codec parameters (key=value)
        bitrate_num = int("".join(c for c in bitrate if c.isdigit()))

        # Select encoder matching the stream's configured codec
        if codec == "h265":
            wf_encoder = "libx265"
            wf_codec_params = [
                "-p", "preset=fast",
                "-p", f"b={bitrate_num * 1000000}",
                "-p", f"x265-params=vbv-bufsize={bitrate_num * 2000}"
                      f":vbv-maxrate={bitrate_num * 1000}"
                      f":min-keyint={gop_size}:keyint={gop_size}",
            ]
        else:
            wf_encoder = "libx264"
            wf_codec_params = [
                "-p", "profile=main",
                "-p", "preset=ultrafast",
                "-p", f"b={bitrate_num * 1000000}",
                "-p", f"g={gop_size}",
            ]

        wf_parts = [
            config.WF_RECORDER_PATH,
            "-y",
            "--no-dmabuf",
            "--muxer", "mpegts",
            "-c", wf_encoder,
            *wf_codec_params,
            "-f", str(framerate),
            "--file", multicast_url,
        ]

        # If we need to scale, use wf-recorder's built-in filter
        if need_scale:
            wf_parts.extend(["--filter", f"scale={width}:{height}"])

        # Write the capture command as a shell script with Wayland env vars.
        import shlex
        wf_str = " ".join(shlex.quote(p) for p in wf_parts)

        wf_log = os.path.join(managed.runtime_dir, "wf-recorder.log")

        pipeline_script = os.path.join(managed.runtime_dir, "capture.sh")
        with open(pipeline_script, "w") as f:
            f.write("#!/bin/sh\n")
            # Export Wayland env vars so wf-recorder can connect to cage
            f.write(f'export XDG_RUNTIME_DIR="{managed.runtime_dir}"\n')
            f.write(f'export WAYLAND_DISPLAY="{managed.wayland_display}"\n')
            f.write(f'export HOME="{os.path.expanduser("~")}"\n')
            # wf-recorder outputs directly to the multicast URL (no pipe)
            f.write(f'{wf_str} 2>{shlex.quote(wf_log)}\n')
        os.chmod(pipeline_script, 0o755)

        logger.info("Starting capture pipeline via script: %s", pipeline_script)
        managed.ffmpeg_proc = await asyncio.create_subprocess_exec(
            "/bin/sh", pipeline_script,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            # Own session so os.killpg() can terminate the entire process tree
            # (shell + wf-recorder + ffmpeg) instead of orphaning children
            start_new_session=True,
            # Deliberately NOT passing env= — let the script set its own env
            # to avoid any asyncio subprocess env inheritance issues
        )

        # Brief wait to confirm the pipeline is running
        await asyncio.sleep(3)

        if managed.ffmpeg_proc.returncode is not None:
            try:
                stderr = await asyncio.wait_for(managed.ffmpeg_proc.stderr.read(), timeout=3)
                logger.error("Capture pipeline exited prematurely (rc=%d): %s",
                             managed.ffmpeg_proc.returncode, stderr.decode()[:500])
            except asyncio.TimeoutError:
                logger.error("Capture pipeline exited prematurely (rc=%d), stderr read timed out",
                             managed.ffmpeg_proc.returncode)
            return False

        # Log diagnostics from wf-recorder
        try:
            if os.path.exists(wf_log):
                with open(wf_log, "r") as lf:
                    content = lf.read(2000)
                if content.strip():
                    logger.info("wf-recorder log:\n%s", content.strip()[:500])
        except OSError:
            pass

        logger.info("Capture pipeline running (shell pid=%d)", managed.ffmpeg_proc.pid)
        return True

    async def _start_vnc(self, managed: ManagedSource) -> bool:
        """Start wayvnc and websockify for VNC preview access.

        wayvnc connects to the cage compositor and serves a VNC endpoint.
        websockify bridges WebSocket connections from noVNC to wayvnc.

        Returns False if either fails — caller decides if this is fatal.
        """
        env = self._base_env(managed)

        # Start wayvnc — cage (wlroots) supports the virtual pointer and keyboard
        # protocols, so input forwarding from VNC clients works correctly.
        wayvnc_cmd = [
            config.WAYVNC_PATH,
            "0.0.0.0", str(managed.vnc_port),
        ]

        logger.info("Starting wayvnc on port %d", managed.vnc_port)
        managed.wayvnc_proc = await asyncio.create_subprocess_exec(
            *wayvnc_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        await asyncio.sleep(1)
        if managed.wayvnc_proc.returncode is not None:
            try:
                stderr = await asyncio.wait_for(managed.wayvnc_proc.stderr.read(), timeout=3)
                logger.error("wayvnc exited prematurely (rc=%d): %s",
                             managed.wayvnc_proc.returncode, stderr.decode()[:500])
            except asyncio.TimeoutError:
                logger.error("wayvnc exited prematurely (rc=%d), stderr read timed out",
                             managed.wayvnc_proc.returncode)
            return False

        # Start websockify — bridges WebSocket (noVNC) to wayvnc
        websockify_cmd = [
            config.WEBSOCKIFY_PATH,
            "--web", config.NOVNC_DIR,
            str(managed.novnc_port),
            f"localhost:{managed.vnc_port}",
        ]

        logger.info("Starting websockify on port %d → VNC port %d",
                     managed.novnc_port, managed.vnc_port)
        managed.websockify_proc = await asyncio.create_subprocess_exec(
            *websockify_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        await asyncio.sleep(0.5)
        if managed.websockify_proc.returncode is not None:
            try:
                stderr = await asyncio.wait_for(managed.websockify_proc.stderr.read(), timeout=3)
                logger.error("websockify exited prematurely (rc=%d): %s",
                             managed.websockify_proc.returncode, stderr.decode()[:500])
            except asyncio.TimeoutError:
                logger.error("websockify exited prematurely (rc=%d), stderr read timed out",
                             managed.websockify_proc.returncode)
            return False

        logger.info("VNC preview ready: wayvnc(pid=%d) websockify(pid=%d)",
                     managed.wayvnc_proc.pid, managed.websockify_proc.pid)
        return True

    async def _start_ydotoold(self, managed: ManagedSource) -> bool:
        """Start the ydotool daemon for keyboard input injection.

        ydotoold listens on a Unix socket and accepts input events from ydotool.
        Only needed for presentation sources (slide navigation via keyboard).
        """
        socket_path = os.path.join(managed.runtime_dir, "ydotool.sock")
        env = self._base_env(managed)

        ydotoold_cmd = [
            config.YDOTOOLD_PATH,
            f"--socket-path={socket_path}",
        ]

        logger.info("Starting ydotoold: socket=%s", socket_path)
        managed.ydotoold_proc = await asyncio.create_subprocess_exec(
            *ydotoold_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        await asyncio.sleep(0.5)
        if managed.ydotoold_proc.returncode is not None:
            try:
                stderr = await asyncio.wait_for(managed.ydotoold_proc.stderr.read(), timeout=3)
                logger.error("ydotoold exited prematurely (rc=%d): %s",
                             managed.ydotoold_proc.returncode, stderr.decode()[:500])
            except asyncio.TimeoutError:
                logger.error("ydotoold exited prematurely (rc=%d), stderr read timed out",
                             managed.ydotoold_proc.returncode)
            return False

        logger.info("ydotoold ready: pid=%d", managed.ydotoold_proc.pid)
        return True

    async def _kill_all_procs(self, managed: ManagedSource):
        """Terminate all processes belonging to a source.

        Sends SIGTERM first, waits briefly, then SIGKILL for any survivors.
        Uses process group kill for shell-script processes (capture.sh) to
        ensure child processes (wf-recorder, ffmpeg) are also terminated —
        otherwise they become orphans and compete with the next pipeline start.
        """
        for proc in managed.all_procs:
            try:
                if proc.returncode is None:
                    # Try killing the entire process group first.
                    # Processes started with start_new_session=True have their
                    # own PGID equal to their PID.
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        proc.terminate()
            except ProcessLookupError:
                pass

        # Give processes 3 seconds to exit gracefully
        await asyncio.sleep(3)

        for proc in managed.all_procs:
            try:
                if proc.returncode is None:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        proc.kill()
            except ProcessLookupError:
                pass

    async def _launch_source(self, stream_id: int, source_mode: str,
                              url: str = "", presentation_file: str = "",
                              capture_audio: bool = False,
                              multicast_address: str = "",
                              multicast_port: int = 5000) -> dict:
        """Core launch sequence shared by browser and presentation sources.

        Orchestrates the sequential startup of all processes, with each step
        depending on the previous one succeeding. If any step fails, all
        previously started processes are killed and the display is released.

        Returns dict with display, vnc_port, novnc_port on success.
        Raises ValueError on failure.
        """
        # Stop any existing source for this stream
        if stream_id in self._active:
            await self.stop_browser(stream_id)

        display_num = self._allocate_display()
        managed = ManagedSource(stream_id, display_num)

        # Read the stream's encoding profile from DB
        db: Session = self._db_factory()
        try:
            stream = db.query(Stream).filter(Stream.id == stream_id).first()
            if not stream:
                self._release_display(display_num)
                raise ValueError(f"Stream {stream_id} not found")
            resolution = stream.resolution or "1920x1080"
        finally:
            db.close()

        try:
            # Step 1: Create isolated runtime directory
            managed.runtime_dir = self._create_runtime_dir(display_num)

            # Step 2: Build application command and environment
            if source_mode == "presentation":
                app_cmd, app_env = self._build_libreoffice_cmd(
                    managed, presentation_file, resolution)
            else:
                app_cmd, app_env = self._build_firefox_cmd(managed, url, resolution)

            # Step 3: Start cage compositor with the application embedded.
            # cage is a wlroots kiosk compositor that runs the app fullscreen.
            # Using cage instead of weston because wf-recorder requires the
            # wlr-screencopy-unstable-v1 protocol (a wlroots extension).
            if not await self._start_cage(managed, resolution, app_cmd, app_env):
                app_name = "LibreOffice Impress" if source_mode == "presentation" else "Firefox"
                raise ValueError(f"Cage compositor with {app_name} failed to start")

            # Wait for the application to initialize inside cage.
            # LibreOffice needs more time than Firefox to render its first frame.
            startup_wait = 8 if source_mode == "presentation" else 5
            logger.info("Waiting %ds for application startup...", startup_wait)
            await asyncio.sleep(startup_wait)

            # Re-detect XWayland display if not found during cage startup.
            # XWayland starts on-demand when an X11 client connects, which may
            # happen after the Wayland socket appears.
            if managed.x_display is None and managed.weston_proc.returncode is None:
                pre_sockets = getattr(managed, '_pre_x_sockets', set())
                managed.x_display = self._detect_x_display(pre_sockets)
                if managed.x_display is not None:
                    logger.info("XWayland display detected (post-startup): :%d", managed.x_display)

            # Check if cage (and its app) are still running after startup wait
            if managed.weston_proc.returncode is not None:
                try:
                    stderr = await asyncio.wait_for(managed.weston_proc.stderr.read(), timeout=3)
                    logger.error("Cage+app exited during startup (rc=%d): %s",
                                 managed.weston_proc.returncode, stderr.decode()[:500])
                except asyncio.TimeoutError:
                    logger.error("Cage+app exited during startup (rc=%d), stderr read timed out",
                                 managed.weston_proc.returncode)
                app_name = "LibreOffice Impress" if source_mode == "presentation" else "Firefox"
                raise ValueError(f"{app_name} failed to start")

            # Step 4: Start VNC preview (wayvnc + websockify)
            # Non-fatal: VNC preview is best-effort. wayvnc may fail due to
            # protocol compatibility issues with the compositor version.
            # Streaming works fine without VNC preview — users just won't see
            # the live preview iframe until VNC is working.
            if not await self._start_vnc(managed):
                logger.warning("VNC preview not available — streaming will work without preview")

            # Step 5: Start capture pipeline (wf-recorder → ffmpeg → multicast)
            db = self._db_factory()
            try:
                stream = db.query(Stream).filter(Stream.id == stream_id).first()
                if not await self._start_capture_pipeline(
                    managed, stream, multicast_address, multicast_port, capture_audio
                ):
                    raise ValueError("Capture pipeline (wf-recorder/ffmpeg) failed to start")
            finally:
                db.close()

            # Note: slide control uses wtype (Wayland-native key sender) invoked
            # on-demand by send_key(). No daemon startup needed — unlike ydotool,
            # wtype connects directly to the compositor via WAYLAND_DISPLAY.

        except (ValueError, OSError) as exc:
            # Any startup failure: kill all processes and release resources
            await self._kill_all_procs(managed)
            self._cleanup_runtime_dir(managed.runtime_dir)
            self._release_display(display_num)
            raise ValueError(f"Source failed to start: {exc}")

        # Register the source and persist port assignments
        self._active[stream_id] = managed

        # Drain stderr pipes in background to prevent buffer fill blocking processes.
        # All subprocesses use stderr=PIPE for failure diagnostics, but long-running
        # processes will block if the pipe buffer fills up.
        for label, proc in [
            ("cage", managed.weston_proc),
            ("capture-pipeline", managed.ffmpeg_proc),
            ("wayvnc", managed.wayvnc_proc),
            ("websockify", managed.websockify_proc),
        ]:
            if proc and proc.stderr:
                asyncio.create_task(_drain_stderr(proc, label))

        db = self._db_factory()
        try:
            browser = db.query(BrowserSource).filter(
                BrowserSource.stream_id == stream_id
            ).first()
            if browser:
                browser.display_number = display_num
                browser.vnc_port = managed.vnc_port
                browser.novnc_port = managed.novnc_port
            db.commit()
        finally:
            db.close()

        # Start background process watcher
        asyncio.create_task(self._watch_processes(managed))

        logger.info(
            "Capture source started: stream=%d mode=%s display=%d vnc=%d novnc=%d",
            stream_id, source_mode, display_num, managed.vnc_port, managed.novnc_port,
        )

        return {
            "display": f":{display_num}",
            "vnc_port": managed.vnc_port,
            "novnc_port": managed.novnc_port,
        }

    async def start_browser(self, stream_id: int, url: str, capture_audio: bool,
                            multicast_address: str, multicast_port: int) -> dict:
        """Launch a native Wayland capture source for browser streaming.

        Starts the full pipeline: cage → Firefox kiosk → wf-recorder → ffmpeg
        → MPEG-TS multicast, with wayvnc/websockify for VNC preview.

        Args:
            stream_id: Database ID of the stream
            url: Web URL to load in Firefox kiosk mode
            capture_audio: Whether to enable PulseAudio capture
            multicast_address: Destination multicast group
            multicast_port: Destination UDP port

        Returns:
            Dict with display, vnc_port, novnc_port
        """
        return await self._launch_source(
            stream_id, "browser",
            url=url, capture_audio=capture_audio,
            multicast_address=multicast_address,
            multicast_port=multicast_port,
        )

    async def start_presentation(self, stream_id: int, presentation_file_path: str,
                                  capture_audio: bool, multicast_address: str,
                                  multicast_port: int) -> dict:
        """Launch a native Wayland capture source for presentation streaming.

        Similar to start_browser() but launches LibreOffice Impress in slideshow
        mode instead of Firefox. Also starts ydotoold for keyboard input injection
        (slide navigation).

        Args:
            stream_id: Database ID of the stream
            presentation_file_path: Absolute path to the slideshow file
            capture_audio: Whether to enable PulseAudio capture
            multicast_address: Destination multicast group
            multicast_port: Destination UDP port

        Returns:
            Dict with display, vnc_port, novnc_port
        """
        if not os.path.isfile(presentation_file_path):
            raise ValueError(f"Presentation file not found: {presentation_file_path}")

        return await self._launch_source(
            stream_id, "presentation",
            presentation_file=presentation_file_path,
            capture_audio=capture_audio,
            multicast_address=multicast_address,
            multicast_port=multicast_port,
        )

    async def send_key(self, stream_id: int, key: str) -> bool:
        """Send a keyboard event to a running capture source.

        For presentation sources (XWayland clients), sends X11 XTEST events
        directly via ctypes — this is the only reliable path since wtype's
        Wayland virtual keyboard events don't bridge to XWayland clients.

        For browser sources (native Wayland), uses wtype which connects
        directly to the compositor via zwp_virtual_keyboard protocol.

        Args:
            stream_id: Database ID of the stream
            key: X11 keysym name (e.g. "Right", "Left", "Home", "End", "Escape")

        Returns:
            True if the key was sent successfully
        """
        managed = self._active.get(stream_id)
        if not managed:
            logger.warning("send_key called for inactive stream %d", stream_id)
            return False

        # If we have an X display (presentation source via XWayland), use XTEST
        if managed.x_display is not None:
            return await self._send_key_x11(managed, stream_id, key)

        # Otherwise use wtype for native Wayland sources
        return await self._send_key_wtype(managed, stream_id, key)

    async def _send_key_x11(self, managed: ManagedSource,
                            stream_id: int, key: str) -> bool:
        """Send a key event via XSendEvent to the LibreOffice presenting window.

        XWayland exposes the XTEST extension but does not actually process its
        events. XSendEvent delivers KeyPress/KeyRelease directly to the target
        window, which XWayland does honor.
        """
        import ctypes

        # X11 keysym values (from X11/keysymdef.h)
        keysym_map = {
            "Right": 0xff53,
            "Left": 0xff51,
            "Home": 0xff50,
            "End": 0xff57,
            "Escape": 0xff1b,
            "space": 0x0020,
            "Return": 0xff0d,
            "Page_Up": 0xff55,
            "Page_Down": 0xff56,
            "Up": 0xff52,
            "Down": 0xff54,
        }

        keysym = keysym_map.get(key)
        if keysym is None:
            logger.warning("Rejected unmapped key '%s' for stream %d", key, stream_id)
            return False

        display_str = f":{managed.x_display}"

        # XKeyEvent structure matching Xlib's layout (x86_64)
        class XKeyEvent(ctypes.Structure):
            _fields_ = [
                ("type", ctypes.c_int),
                ("serial", ctypes.c_ulong),
                ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p),
                ("window", ctypes.c_ulong),
                ("root", ctypes.c_ulong),
                ("subwindow", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("x", ctypes.c_int),
                ("y", ctypes.c_int),
                ("x_root", ctypes.c_int),
                ("y_root", ctypes.c_int),
                ("state", ctypes.c_uint),
                ("keycode", ctypes.c_uint),
                ("same_screen", ctypes.c_int),
            ]

        try:
            xlib = ctypes.cdll.LoadLibrary("libX11.so.6")

            # Declare proper function signatures — critical on x86_64 where
            # pointers are 64-bit but ctypes defaults to 32-bit int args.
            xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
            xlib.XOpenDisplay.restype = ctypes.c_void_p
            xlib.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            xlib.XKeysymToKeycode.restype = ctypes.c_int
            xlib.XFlush.argtypes = [ctypes.c_void_p]
            xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
            xlib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            xlib.XDefaultRootWindow.restype = ctypes.c_ulong
            xlib.XQueryTree.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
                ctypes.POINTER(ctypes.c_uint),
            ]
            xlib.XQueryTree.restype = ctypes.c_int
            xlib.XFetchName.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_char_p),
            ]
            xlib.XFetchName.restype = ctypes.c_int
            xlib.XFree.argtypes = [ctypes.c_void_p]
            xlib.XSetInputFocus.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong,
            ]
            xlib.XSendEvent.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                ctypes.c_long, ctypes.c_void_p,
            ]
            xlib.XSendEvent.restype = ctypes.c_int

            display = xlib.XOpenDisplay(display_str.encode())
            if not display:
                logger.warning("Could not open X display %s for stream %d",
                               display_str, stream_id)
                return False

            try:
                root = xlib.XDefaultRootWindow(display)

                # Find the "Presenting:" window
                root_ret = ctypes.c_ulong()
                parent = ctypes.c_ulong()
                children = ctypes.POINTER(ctypes.c_ulong)()
                nchildren = ctypes.c_uint()
                xlib.XQueryTree(display, root, ctypes.byref(root_ret),
                                ctypes.byref(parent), ctypes.byref(children),
                                ctypes.byref(nchildren))

                presenting_win = None
                for i in range(nchildren.value):
                    wid = children[i]
                    name = ctypes.c_char_p()
                    xlib.XFetchName(display, wid, ctypes.byref(name))
                    if name.value:
                        wname = name.value.decode(errors="replace")
                        xlib.XFree(name)
                        if wname.startswith("Presenting:"):
                            presenting_win = wid
                            break

                if nchildren.value > 0:
                    xlib.XFree(children)

                if not presenting_win:
                    logger.warning("No 'Presenting:' window found on display %s "
                                   "for stream %d", display_str, stream_id)
                    return False

                # Set focus to the presenting window first
                xlib.XSetInputFocus(display, presenting_win, 2, 0)

                keycode = xlib.XKeysymToKeycode(display, keysym)
                if keycode == 0:
                    logger.warning("No keycode for keysym 0x%x on display %s",
                                   keysym, display_str)
                    return False

                # X11 constants
                KEY_PRESS = 2
                KEY_RELEASE = 3
                KEY_PRESS_MASK = (1 << 0)
                KEY_RELEASE_MASK = (1 << 1)

                # Build and send KeyPress event
                ev = XKeyEvent()
                ev.type = KEY_PRESS
                ev.serial = 0
                ev.send_event = 1
                ev.display = display
                ev.window = presenting_win
                ev.root = root
                ev.subwindow = 0
                ev.time = 0
                ev.x = 1
                ev.y = 1
                ev.x_root = 1
                ev.y_root = 1
                ev.state = 0
                ev.keycode = keycode
                ev.same_screen = 1

                xlib.XSendEvent(display, presenting_win, 0,
                                KEY_PRESS_MASK, ctypes.byref(ev))

                # Send KeyRelease
                ev.type = KEY_RELEASE
                xlib.XSendEvent(display, presenting_win, 0,
                                KEY_RELEASE_MASK, ctypes.byref(ev))

                xlib.XFlush(display)

                logger.info("X11 XSendEvent: key='%s' keysym=0x%x keycode=%d "
                           "display=%s stream=%d win=%d",
                           key, keysym, keycode, display_str, stream_id,
                           presenting_win)
            finally:
                xlib.XCloseDisplay(display)

        except (OSError, Exception) as exc:
            logger.warning("X11 key send failed for stream %d: %s", stream_id, exc)
            return False

        return True

    async def _send_key_wtype(self, managed: ManagedSource,
                              stream_id: int, key: str) -> bool:
        """Send a key event via wtype (Wayland virtual keyboard protocol)."""
        key_map = {
            "Right": "Right",
            "Left": "Left",
            "Home": "Home",
            "End": "End",
            "Escape": "Escape",
            "space": "space",
            "Return": "Return",
            "Page_Up": "Prior",
            "Page_Down": "Next",
            "Up": "Up",
            "Down": "Down",
        }

        xkb_key = key_map.get(key)
        if not xkb_key:
            logger.warning("Rejected unmapped key '%s' for stream %d", key, stream_id)
            return False

        env = self._base_env(managed)

        try:
            proc = await asyncio.create_subprocess_exec(
                config.WTYPE_PATH, "-k", xkb_key,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.warning("wtype key send failed for stream %d: %s",
                               stream_id, stderr.decode()[:200])
                return False
        except (OSError, FileNotFoundError) as exc:
            logger.warning("wtype not available for stream %d: %s", stream_id, exc)
            return False

        logger.debug("Sent key '%s' (xkb %s) to stream %d", key, xkb_key, stream_id)
        return True

    async def stop_browser(self, stream_id: int):
        """Stop all processes for a capture source and clean up resources.

        Terminates all native processes (cage, Firefox/LO, wf-recorder, ffmpeg,
        wayvnc, websockify, ydotoold), removes the runtime directory, and clears
        port assignments in the database.
        """
        managed = self._active.get(stream_id)
        if not managed:
            return

        managed.should_stop = True
        logger.info("Stopping capture source for stream %d", stream_id)

        await self._kill_all_procs(managed)
        self._cleanup_runtime_dir(managed.runtime_dir)
        self._release_display(managed.display_number)
        del self._active[stream_id]

        # Clear port assignments in DB
        db: Session = self._db_factory()
        try:
            browser = db.query(BrowserSource).filter(
                BrowserSource.stream_id == stream_id
            ).first()
            if browser:
                browser.display_number = None
                browser.vnc_port = None
                browser.novnc_port = None
            db.commit()
        finally:
            db.close()

        logger.info("Capture source stopped: stream=%d", stream_id)

    async def _watch_processes(self, managed: ManagedSource):
        """Monitor all processes and handle unexpected exits.

        Checks every 5 seconds whether any critical process has exited. If so
        and should_stop is False (not user-initiated), marks the stream as ERROR
        in the database and cleans up all remaining processes.
        """
        # Critical processes — if any of these die, the stream is broken
        critical_procs = [
            ("cage", lambda: managed.weston_proc),
            ("capture-pipeline", lambda: managed.ffmpeg_proc),
        ]

        try:
            while not managed.should_stop:
                await asyncio.sleep(5)
                for name, get_proc in critical_procs:
                    proc = get_proc()
                    if proc and proc.returncode is not None:
                        if not managed.should_stop:
                            logger.warning(
                                "Process %s (pid=%d) exited unexpectedly for stream %d (rc=%d)",
                                name, proc.pid, managed.stream_id, proc.returncode,
                            )
                            # Clean up everything
                            await self._kill_all_procs(managed)
                            self._cleanup_runtime_dir(managed.runtime_dir)
                            if managed.stream_id in self._active:
                                self._release_display(managed.display_number)
                                del self._active[managed.stream_id]
                            # Mark stream as errored in DB
                            db: Session = self._db_factory()
                            try:
                                stream = db.query(Stream).filter(
                                    Stream.id == managed.stream_id
                                ).first()
                                if stream:
                                    stream.status = StreamStatus.ERROR
                                    stream.ffmpeg_pid = None
                                db.commit()
                            finally:
                                db.close()
                        return
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Error watching processes for stream %d", managed.stream_id)

    def get_browser_pids(self, stream_id: int) -> list:
        """Get all process PIDs for resource monitoring via psutil.

        Returns PIDs of all running processes belonging to this source.
        Unlike the container approach (which returned a single container init PID),
        this returns individual process PIDs directly.
        """
        managed = self._active.get(stream_id)
        if not managed:
            return []
        pids = []
        for proc in managed.all_procs:
            try:
                if proc.returncode is None and proc.pid:
                    pids.append(proc.pid)
            except (AttributeError, ProcessLookupError):
                pass
        return pids

    def get_status(self, stream_id: int) -> dict:
        """Get the runtime status of a capture source for the API."""
        managed = self._active.get(stream_id)
        if not managed:
            return {"active": False}
        return {
            "active": True,
            "display": f":{managed.display_number}",
            "vnc_port": managed.vnc_port,
            "novnc_port": managed.novnc_port,
            "pids": self.get_browser_pids(stream_id),
        }

    async def stop_all(self):
        """Kill all capture sources during application shutdown.

        Unlike stop_browser(), this does NOT update DB status — streams stay
        marked as RUNNING so restore_sessions() can restart them on next boot.
        """
        for sid in list(self._active.keys()):
            managed = self._active[sid]
            managed.should_stop = True
            await self._kill_all_procs(managed)
            self._cleanup_runtime_dir(managed.runtime_dir)
            self._release_display(managed.display_number)
            logger.info("Capture source killed (shutdown): stream=%d", sid)
        self._active.clear()

    async def restore_sessions(self):
        """Restore capture sources that were marked RUNNING before a service restart.

        Queries DB for BROWSER/PRESENTATION streams with status=RUNNING and
        attempts to relaunch them. If a source fails to start, the stream is
        marked as ERROR.
        """
        db: Session = self._db_factory()
        try:
            container_streams = db.query(Stream).filter(
                Stream.source_type.in_([
                    StreamSourceType.BROWSER,
                    StreamSourceType.PRESENTATION,
                ]),
                Stream.status == StreamStatus.RUNNING,
            ).all()

            for stream in container_streams:
                try:
                    if stream.source_type == StreamSourceType.PRESENTATION:
                        presentation = None
                        if stream.browser_source and stream.browser_source.presentation_id:
                            presentation = db.query(Presentation).filter(
                                Presentation.id == stream.browser_source.presentation_id
                            ).first()
                        if presentation and presentation.file_path:
                            logger.info("Restoring presentation source for stream %d: %s",
                                        stream.id, presentation.file_path)
                            await self.start_presentation(
                                stream.id,
                                presentation.file_path,
                                stream.browser_source.capture_audio if stream.browser_source else False,
                                stream.multicast_address,
                                stream.multicast_port,
                            )
                            stream.status = StreamStatus.RUNNING
                        else:
                            logger.error("Cannot restore presentation stream %d: no file path",
                                         stream.id)
                            stream.status = StreamStatus.ERROR
                    elif stream.source_type == StreamSourceType.BROWSER:
                        if stream.browser_source and stream.browser_source.url:
                            logger.info("Restoring browser source for stream %d: %s",
                                        stream.id, stream.browser_source.url)
                            await self.start_browser(
                                stream.id,
                                stream.browser_source.url,
                                stream.browser_source.capture_audio,
                                stream.multicast_address,
                                stream.multicast_port,
                            )
                            stream.status = StreamStatus.RUNNING
                        else:
                            logger.error("Cannot restore browser stream %d: no URL", stream.id)
                            stream.status = StreamStatus.ERROR
                except Exception as exc:
                    logger.error("Failed to restore stream %d: %s", stream.id, exc)
                    stream.status = StreamStatus.ERROR
            db.commit()
        finally:
            db.close()
