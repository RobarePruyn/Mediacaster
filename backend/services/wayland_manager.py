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
        # Create exactly one virtual output
        env["WLR_HEADLESS_OUTPUTS"] = "1"
        # Force pixman (software) renderer — no GPU/DRM render node on headless servers
        env["WLR_RENDERER"] = "pixman"
        # Merge any application-specific env vars (e.g., MOZ_ENABLE_WAYLAND for Firefox)
        if app_env:
            env.update(app_env)

        cage_cmd = [
            config.CAGE_PATH,
            "--",
        ] + app_cmd

        logger.info("Starting cage: %s (runtime=%s)", " ".join(cage_cmd), managed.runtime_dir)
        managed.weston_proc = await asyncio.create_subprocess_exec(
            *cage_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        # Wait for the Wayland socket to appear (indicates cage is ready)
        socket_path = os.path.join(managed.runtime_dir, managed.wayland_display)
        for attempt in range(30):
            await asyncio.sleep(0.25)
            if os.path.exists(socket_path):
                logger.info("Cage ready: socket=%s pid=%d", socket_path, managed.weston_proc.pid)
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
            # Force software GL rendering — no GPU on headless servers.
            # Without this, XWayland glamor fails with "GBM Wayland interfaces
            # not available" because the pixman renderer doesn't support GBM.
            "LIBGL_ALWAYS_SOFTWARE": "1",
        }

        # Call soffice.bin directly to bypass the oosplash launcher wrapper.
        # The default /usr/bin/libreoffice → oosplash chain tries to actually
        # connect to the X11 DISPLAY (not just check the env var), which fails
        # in a headless Wayland-only environment. soffice.bin skips that check
        # and honors SAL_USE_VCLPLUGIN + GDK_BACKEND directly.
        soffice_bin = config.LIBREOFFICE_PATH.replace("/soffice", "/soffice.bin")
        if not soffice_bin.endswith(".bin"):
            # Fallback: if LIBREOFFICE_PATH doesn't end with /soffice (e.g. /usr/bin/libreoffice),
            # look for soffice.bin alongside it or in the standard TDF install path
            candidate = Path(config.LIBREOFFICE_PATH).parent / "soffice.bin"
            if not candidate.exists():
                # TDF builds install to /opt/libreofficeXX.Y/program/
                for tdf_dir in sorted(Path("/opt").glob("libreoffice*/program/soffice.bin"), reverse=True):
                    candidate = tdf_dir
                    break
            soffice_bin = str(candidate) if candidate.exists() else config.LIBREOFFICE_PATH

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
</oor:items>
""")

        # Wrap soffice.bin in a shell script that waits for XWayland to be ready.
        # cage starts XWayland asynchronously — if soffice.bin runs immediately,
        # it may fail with "no suitable windowing system found" because the X11
        # display isn't accepting connections yet. The wrapper polls with xdpyinfo
        # (falls back to a fixed sleep if xdpyinfo isn't available).
        wrapper_script = os.path.join(managed.runtime_dir, "lo_wrapper.sh")
        with open(wrapper_script, "w") as f:
            f.write(f"""#!/bin/sh
# Wait for XWayland DISPLAY to become available
for i in $(seq 1 20); do
    if xdpyinfo >/dev/null 2>&1; then
        break
    fi
    sleep 0.25
done
exec {soffice_bin} --norestore --nofirststartwizard -env:UserInstallation=file://{lo_profile} --show '{file_path}'
""")
        os.chmod(wrapper_script, 0o755)

        cmd = ["/bin/sh", wrapper_script]

        logger.info("LibreOffice command prepared: %s (binary: %s)", file_path, soffice_bin)
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
        screencopy protocol and pipes raw video frames to ffmpeg. ffmpeg
        encodes (H.264/H.265), muxes to MPEG-TS, and sends via UDP multicast.

        This two-process pipeline replaces the single ffmpeg x11grab command
        used in the container approach, but avoids the container overhead.
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

        # --- Start wf-recorder: screencopy → raw video pipe to stdout ---
        wf_cmd = [
            config.WF_RECORDER_PATH,
            "--muxer", "rawvideo",
            "--codec", "rawvideo",
            "--pixel-format", "bgr0",
            "-f", str(framerate),
            "--file", "/dev/stdout",
        ]

        # Create a raw OS pipe for wf-recorder stdout → ffmpeg stdin.
        # We cannot use asyncio.subprocess.PIPE for wf-recorder stdout and then
        # pass it as ffmpeg stdin — uvloop wraps it as a StreamReader which lacks
        # fileno(). An OS-level pipe gives both processes raw file descriptors.
        video_pipe_r, video_pipe_w = os.pipe()

        logger.info("Starting wf-recorder: %s", " ".join(wf_cmd))
        managed.wf_recorder_proc = await asyncio.create_subprocess_exec(
            *wf_cmd,
            stdout=video_pipe_w,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        # Close the write end in our process — wf-recorder owns it now
        os.close(video_pipe_w)

        # --- Build ffmpeg command: raw video from pipe → encode → multicast ---
        ffmpeg_cmd = [
            config.FFMPEG_PATH, "-y",
            # Raw video input from wf-recorder pipe
            "-thread_queue_size", "512",
            "-f", "rawvideo",
            "-pix_fmt", "bgr0",
            "-video_size", resolution,
            "-framerate", str(framerate),
            "-i", "pipe:0",
        ]

        # Audio input: PulseAudio sink monitor or silent track
        pulse_sink = None
        if capture_audio:
            pulse_sink = await self._start_audio(managed)

        if pulse_sink:
            ffmpeg_cmd.extend([
                "-thread_queue_size", "512",
                "-f", "pulse",
                "-i", f"{pulse_sink}.monitor",
            ])
        else:
            # Generate silent audio — MPEG-TS receivers expect both audio and video
            ffmpeg_cmd.extend([
                "-f", "lavfi",
                "-i", "anullsrc=r=48000:cl=stereo",
            ])

        # Constant frame rate enforcement
        ffmpeg_cmd.extend(["-vsync", "cfr", "-r", str(framerate)])

        # Parse bitrate number for VBV calculations
        bitrate_num = int("".join(c for c in bitrate if c.isdigit()))

        # Video encoder selection based on codec
        if codec == "h265":
            bufsize_kbps = bitrate_num * 1000 * 2
            maxrate_kbps = bitrate_num * 1000
            ffmpeg_cmd.extend([
                "-c:v", "libx265",
                "-profile:v", "main",
                "-preset", "fast",
                "-b:v", bitrate,
                "-x265-params",
                f"vbv-bufsize={bufsize_kbps}:vbv-maxrate={maxrate_kbps}"
                f":nal-hrd=cbr:min-keyint={gop_size}:keyint={gop_size}",
                "-pix_fmt", "yuv420p",
            ])
        else:
            bufsize = f"{bitrate_num}M"
            ffmpeg_cmd.extend([
                "-c:v", "libx264",
                "-profile:v", "main",
                "-preset", "ultrafast",
                "-b:v", bitrate,
                "-minrate", bitrate,
                "-maxrate", bitrate,
                "-bufsize", bufsize,
                "-nal-hrd", "cbr",
                "-pix_fmt", "yuv420p",
                "-g", str(gop_size),
            ])

        # Audio encoding + MPEG-TS output
        ffmpeg_cmd.extend([
            "-c:a", "aac",
            "-b:a", config.BROWSER_SOURCE_AUDIO_BITRATE,
            "-ac", "2",
            "-ar", "48000",
            "-f", "mpegts",
            "-mpegts_transport_stream_id", "1",
            "-flush_packets", "1",
            multicast_url,
        ])

        logger.info("Starting ffmpeg encoder → %s", multicast_url)
        managed.ffmpeg_proc = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdin=video_pipe_r,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        # Close the read end in our process — ffmpeg owns it now
        os.close(video_pipe_r)

        # Brief wait to confirm both processes are running
        await asyncio.sleep(1)

        if managed.wf_recorder_proc.returncode is not None:
            try:
                stderr = await asyncio.wait_for(managed.wf_recorder_proc.stderr.read(), timeout=3)
                logger.error("wf-recorder exited prematurely (rc=%d): %s",
                             managed.wf_recorder_proc.returncode, stderr.decode()[:500])
            except asyncio.TimeoutError:
                logger.error("wf-recorder exited prematurely (rc=%d), stderr read timed out",
                             managed.wf_recorder_proc.returncode)
            return False
        if managed.ffmpeg_proc.returncode is not None:
            try:
                stderr = await asyncio.wait_for(managed.ffmpeg_proc.stderr.read(), timeout=3)
                logger.error("ffmpeg exited prematurely (rc=%d): %s",
                             managed.ffmpeg_proc.returncode, stderr.decode()[:500])
            except asyncio.TimeoutError:
                logger.error("ffmpeg exited prematurely (rc=%d), stderr read timed out",
                             managed.ffmpeg_proc.returncode)
            return False

        logger.info("Capture pipeline running: wf-recorder(pid=%d) → ffmpeg(pid=%d)",
                     managed.wf_recorder_proc.pid, managed.ffmpeg_proc.pid)
        return True

    async def _start_vnc(self, managed: ManagedSource) -> bool:
        """Start wayvnc and websockify for VNC preview access.

        wayvnc connects to the cage compositor and serves a VNC endpoint.
        websockify bridges WebSocket connections from noVNC to wayvnc.

        Returns False if either fails — caller decides if this is fatal.
        """
        env = self._base_env(managed)

        # Start wayvnc — --disable-input avoids virtual pointer protocol issues
        # doesn't support zwp_virtual_pointer_manager_v1
        wayvnc_cmd = [
            config.WAYVNC_PATH,
            "--disable-input",
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
        """
        for proc in managed.all_procs:
            try:
                if proc.returncode is None:
                    proc.terminate()
            except ProcessLookupError:
                pass

        # Give processes 3 seconds to exit gracefully
        await asyncio.sleep(3)

        for proc in managed.all_procs:
            try:
                if proc.returncode is None:
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

            # Step 6: Start ydotoold for presentation sources (keyboard input)
            if source_mode == "presentation":
                if not await self._start_ydotoold(managed):
                    logger.warning("ydotoold failed — slide control will not work")
                    # Non-fatal: streaming still works, just no keyboard input

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
            ("wf-recorder", managed.wf_recorder_proc),
            ("ffmpeg", managed.ffmpeg_proc),
            ("wayvnc", managed.wayvnc_proc),
            ("websockify", managed.websockify_proc),
            ("ydotoold", managed.ydotoold_proc),
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
        """Send a keyboard event to a running source via ydotool.

        Used to control LibreOffice Impress slideshow navigation from the API.
        Maps X11 keysym names (used by the existing API) to Linux evdev key
        codes that ydotool understands.

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

        # Map X11 keysym names to Linux evdev key codes for ydotool.
        # ydotool key syntax: "<code>:1" = press, "<code>:0" = release
        # Key codes from linux/input-event-codes.h
        key_map = {
            "Right": "106",      # KEY_RIGHT
            "Left": "105",       # KEY_LEFT
            "Home": "102",       # KEY_HOME
            "End": "107",        # KEY_END
            "Escape": "1",       # KEY_ESC
            "space": "57",       # KEY_SPACE
            "Return": "28",      # KEY_ENTER
            "Page_Up": "104",    # KEY_PAGEUP
            "Page_Down": "109",  # KEY_PAGEDOWN
            "Up": "103",         # KEY_UP
            "Down": "108",       # KEY_DOWN
        }

        evdev_code = key_map.get(key)
        if not evdev_code:
            logger.warning("Rejected unmapped key '%s' for stream %d", key, stream_id)
            return False

        env = self._base_env(managed)
        socket_path = os.path.join(managed.runtime_dir, "ydotool.sock")
        env["YDOTOOL_SOCKET"] = socket_path

        try:
            proc = await asyncio.create_subprocess_exec(
                config.YDOTOOL_PATH, "key",
                f"{evdev_code}:1", f"{evdev_code}:0",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.warning("ydotool key send failed for stream %d: %s",
                               stream_id, stderr.decode()[:200])
                return False
        except (OSError, FileNotFoundError) as exc:
            logger.warning("ydotool not available for stream %d: %s", stream_id, exc)
            return False

        logger.debug("Sent key '%s' (evdev %s) to stream %d", key, evdev_code, stream_id)
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
            ("wf-recorder", lambda: managed.wf_recorder_proc),
            ("ffmpeg", lambda: managed.ffmpeg_proc),
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
