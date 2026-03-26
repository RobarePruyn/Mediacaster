"""
Capture Source Manager — Podman container-based virtual display capture for multicast streaming.

Manages two container-based source types that share the same container image and
capture pipeline (Xvfb + x11grab + ffmpeg → MPEG-TS UDP multicast):

  BROWSER:      Firefox kiosk mode renders a web URL
  PRESENTATION: LibreOffice Impress renders a slideshow natively (preserving
                animations, transitions, and embedded media)

Each source runs as an isolated Podman container based on AlmaLinux 9,
providing the X11 stack (Xvfb, x11vnc, xdotool) that AlmaLinux 10 dropped
(AL10 is Wayland-only, but we need X11 for reliable screen capture via x11grab).

Container per source provides:
    - Xvfb virtual display at the configured resolution
    - Firefox (browser mode) or LibreOffice Impress --show (presentation mode)
    - x11vnc for interactive VNC control
    - websockify/noVNC for web-based preview (embedded in the UI via iframe)
    - ffmpeg x11grab capturing the virtual display into MPEG-TS UDP multicast
    - Optional PulseAudio audio capture

Presentation slide control is done via xdotool key events sent through
``podman exec`` — LibreOffice Impress responds to Left/Right/Home/End/Escape.

Uses --network=host so the container can output directly to multicast addresses
without NAT or port-mapping complications. This is required because multicast
routing operates at Layer 3 and container bridge networks don't forward multicast.

Port allocation scheme:
    Each container gets a unique display number (starting at 50), which determines
    its VNC port (5950 + display) and noVNC/websockify port (6080 + display).
    These ranges must be open in the firewall (see deploy.sh).
"""

import asyncio
import logging
import json
import os
import subprocess
from typing import Dict, Optional
from sqlalchemy.orm import Session
from backend import config
from backend.models import BrowserSource, Stream, StreamStatus, StreamSourceType, Presentation
from backend.services.encoding_profiles import get_effective_bitrate, get_effective_gop_size

logger = logging.getLogger("browser_manager")

# Container image name — built by deploy.sh or ensure_image_built() from container/Containerfile
CONTAINER_IMAGE = "mcs-browser-source:latest"
# Prefix for container names — each gets "{prefix}{stream_id}" for easy identification
CONTAINER_NAME_PREFIX = "mcs-browser-"


def _detect_host_multicast_ip() -> str:
    """
    Detect the IP address of the host's primary network interface.

    Used to set localaddr= in ffmpeg's UDP multicast output URL, ensuring
    packets are sent from the correct NIC rather than relying on the container's
    routing table (which may not have the multicast route even with --network=host).

    Falls back to empty string if detection fails, in which case ffmpeg uses
    the kernel's default routing (which works if the multicast route exists).
    """
    # ip lives in /usr/sbin on RHEL/AlmaLinux, which may not be in the
    # systemd service's PATH. Try both common locations.
    ip_cmd = "/usr/sbin/ip"
    if not os.path.exists(ip_cmd):
        ip_cmd = "ip"  # Fall back to PATH lookup

    try:
        result = subprocess.run(
            [ip_cmd, "route", "get", "1.1.1.1"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            # Output format: "1.1.1.1 via <gw> dev <iface> src <ip> uid ..."
            parts = result.stdout.split()
            if "src" in parts:
                return parts[parts.index("src") + 1]
        logger.warning("ip route get returned code %d: %s", result.returncode, result.stderr)
    except (subprocess.TimeoutExpired, OSError, IndexError, ValueError) as exc:
        logger.warning("Failed to detect host multicast interface IP: %s", exc)
    return ""

# Port allocation bases — each container offsets from these by its display number
# Display :50 -> VNC 6000, noVNC 6130; Display :51 -> VNC 6001, noVNC 6131; etc.
DISPLAY_BASE = 50
VNC_PORT_BASE = 5950
NOVNC_PORT_BASE = 6080


class ManagedBrowser:
    """
    Tracks a running browser source container's identity and port assignments.

    The display number is the key to all port calculations — VNC and noVNC ports
    are deterministic offsets from their respective bases.
    """
    def __init__(self, stream_id: int, display_number: int, container_id: str):
        self.stream_id = stream_id
        self.display_number = display_number
        self.container_id = container_id
        # Set to True when stop_browser() is called to prevent the watcher from flagging an error
        self.should_stop = False

    @property
    def container_name(self):
        """Podman container name for this browser source (e.g. 'mcs-browser-3')."""
        return f"{CONTAINER_NAME_PREFIX}{self.stream_id}"

    @property
    def vnc_port(self):
        """VNC port for direct VNC client connections (x11vnc listens here)."""
        return VNC_PORT_BASE + self.display_number

    @property
    def novnc_port(self):
        """Websockify port for noVNC browser-based VNC access (embedded in the UI)."""
        return NOVNC_PORT_BASE + self.display_number


class BrowserManager:
    """
    Manages Podman containers for browser source capture and multicast output.

    Handles container lifecycle (start, stop, crash detection), display number
    allocation to prevent port conflicts, and DB synchronization of port assignments.
    """

    def __init__(self, db_session_factory):
        # Maps stream_id -> ManagedBrowser for all running browser containers
        self._active: Dict[int, ManagedBrowser] = {}
        self._db_factory = db_session_factory
        # Tracks which X display numbers are in use to prevent port collisions
        self._used_displays = set()

    def _allocate_display(self) -> int:
        """
        Allocate the next available X display number.

        Starts at DISPLAY_BASE (50) and increments until finding one not in use.
        This determines the VNC and noVNC ports for the container.

        Returns:
            An unused display number (e.g. 50, 51, 52...)
        """
        display = DISPLAY_BASE
        while display in self._used_displays:
            display += 1
        self._used_displays.add(display)
        return display

    def _release_display(self, display: int):
        """Return a display number to the available pool when a container stops."""
        self._used_displays.discard(display)

    def is_active(self, stream_id: int) -> bool:
        """Check if a browser source container is running for the given stream."""
        return stream_id in self._active

    def get_novnc_port(self, stream_id: int) -> Optional[int]:
        """
        Get the noVNC websockify port for a running browser source.

        Used by the streams API to tell the frontend which port to connect the
        noVNC iframe to for live browser preview/interaction.

        Args:
            stream_id: Database ID of the stream

        Returns:
            Port number, or None if the browser source isn't running
        """
        managed = self._active.get(stream_id)
        return managed.novnc_port if managed else None

    async def _run_cmd(self, cmd: list) -> tuple:
        """
        Run a shell command asynchronously, capturing stdout and stderr.

        Args:
            cmd: Command and arguments as a list

        Returns:
            Tuple of (return_code, stdout_string, stderr_string)
        """
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode, stdout.decode().strip(), stderr.decode().strip()

    async def ensure_image_built(self) -> bool:
        """
        Ensure the browser source container image exists, building it if necessary.

        Checks for the image first (fast path), then builds from the Containerfile
        if not found. Looks for the Containerfile relative to this source file first
        (development), then at the installed location (production /opt/multicast-streamer/).

        Returns:
            True if the image is available, False if the build failed
        """
        # Fast path: check if the image already exists in the local Podman store
        rc, stdout, _ = await self._run_cmd([
            "sudo", "podman", "image", "exists", CONTAINER_IMAGE
        ])
        if rc == 0:
            logger.info("Container image %s already exists", CONTAINER_IMAGE)
            return True

        # Image not found — attempt to build it from the Containerfile
        # First try the development location (relative to this source file)
        container_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "container"
        )
        # Fall back to the production installed location
        if not os.path.exists(os.path.join(container_dir, "Containerfile")):
            container_dir = "/opt/multicast-streamer/container"

        if not os.path.exists(os.path.join(container_dir, "Containerfile")):
            logger.error("Containerfile not found — cannot build browser source image")
            return False

        logger.info("Building container image %s from %s...", CONTAINER_IMAGE, container_dir)
        rc, stdout, stderr = await self._run_cmd([
            "sudo", "podman", "build",
            "-t", CONTAINER_IMAGE,
            "-f", os.path.join(container_dir, "Containerfile"),
            container_dir,
        ])
        if rc != 0:
            logger.error("Container image build failed: %s", stderr)
            return False

        logger.info("Container image built successfully")
        return True

    def _get_container_encoding_env(self, stream: Stream) -> list:
        """Build encoding-related env vars for a container from the stream's profile.

        Reads per-stream encoding settings (resolution, codec, framerate, bitrate)
        and returns them as `-e KEY=VALUE` pairs for the podman run command.
        Also selects the appropriate encoder preset based on codec and source type.

        Args:
            stream: Stream ORM object with encoding columns populated

        Returns:
            List of ["-e", "KEY=VALUE", ...] strings for podman run
        """
        resolution = stream.resolution or "1920x1080"
        codec = stream.codec or "h264"
        framerate = stream.framerate or 30
        bitrate = get_effective_bitrate(resolution, framerate, codec, stream.video_bitrate)
        gop_size = get_effective_gop_size(framerate, stream.gop_size)

        # Select preset: H.265 needs more time so we use "fast" instead of "ultrafast".
        # For browser/presentation live capture, speed matters — use the fastest
        # preset that still produces acceptable quality at the given codec.
        if codec == "h265":
            preset = "fast"
        else:
            preset = "ultrafast"

        return [
            "-e", f"RESOLUTION={resolution}",
            "-e", f"FRAMERATE={framerate}",
            "-e", f"VIDEO_CODEC={codec}",
            "-e", f"VIDEO_BITRATE={bitrate}",
            "-e", f"GOP_SIZE={gop_size}",
            "-e", f"ENCODER_PRESET={preset}",
            "-e", f"AUDIO_BITRATE={config.BROWSER_SOURCE_AUDIO_BITRATE}",
        ]

    async def start_browser(self, stream_id: int, url: str, capture_audio: bool,
                            multicast_address: str, multicast_port: int) -> dict:
        """
        Launch a Podman container for browser source capture and multicast output.

        The container runs the full stack defined in container/entrypoint.sh:
        Xvfb -> Firefox kiosk -> x11vnc -> websockify -> ffmpeg x11grab -> UDP multicast.

        Encoding settings (resolution, codec, framerate, bitrate) are read from the
        stream's per-stream encoding profile rather than global config.

        Key Podman flags:
          --network=host: Required for multicast — bridge networks don't forward multicast traffic
          --shm-size=512m: Firefox needs large shared memory for its multi-process rendering
                           (default 64MB causes crashes on complex web pages)

        Args:
            stream_id: Database ID of the stream
            url: Web URL to load in Firefox kiosk mode
            capture_audio: Whether to enable PulseAudio capture from the browser
            multicast_address: Destination multicast group (e.g. "239.1.1.1")
            multicast_port: Destination UDP port

        Returns:
            Dict with container_id, display, vnc_port, novnc_port

        Raises:
            ValueError: If the container image is unavailable or container fails to start
        """

        # Stop any existing container for this stream before starting a new one
        if stream_id in self._active:
            await self.stop_browser(stream_id)

        # Ensure the container image is built (may trigger a build on first use)
        if not await self.ensure_image_built():
            raise ValueError("Browser source container image not available. "
                             "Check logs for build errors.")

        # Allocate a unique display number, which determines all port assignments
        display_num = self._allocate_display()
        vnc_port = VNC_PORT_BASE + display_num
        novnc_port = NOVNC_PORT_BASE + display_num
        container_name = f"{CONTAINER_NAME_PREFIX}{stream_id}"

        # Remove any stale container with the same name left over from a crash or unclean shutdown
        await self._run_cmd(["sudo", "podman", "rm", "-f", container_name])

        # Detect the host's primary interface IP for explicit multicast binding.
        # Even with --network=host, the container's ffmpeg may not route multicast
        # correctly without an explicit localaddr= in the UDP URL.
        host_multicast_ip = _detect_host_multicast_ip()
        if host_multicast_ip:
            logger.info("Multicast interface binding: localaddr=%s", host_multicast_ip)
        else:
            logger.warning("Could not detect host IP for multicast binding — "
                           "ffmpeg will rely on kernel routing table")

        # Read per-stream encoding profile from the database
        db: Session = self._db_factory()
        try:
            stream = db.query(Stream).filter(Stream.id == stream_id).first()
            if not stream:
                self._release_display(display_num)
                raise ValueError(f"Stream {stream_id} not found")
            encoding_env = self._get_container_encoding_env(stream)
        finally:
            db.close()

        # Build the podman run command with all environment variables the entrypoint.sh expects
        podman_cmd = [
            "sudo", "podman", "run",
            "--detach",
            "--name", container_name,
            "--network=host",              # Required: multicast doesn't work over bridge networks
            "--shm-size=512m",             # Required: Firefox crashes with default 64MB shm
            "-e", f"DISPLAY_NUM={display_num}",
            "-e", f"URL={url}",
            "-e", f"MULTICAST_ADDR={multicast_address}",
            "-e", f"MULTICAST_PORT={multicast_port}",
            "-e", f"MULTICAST_TTL={config.MULTICAST_TTL}",
            "-e", f"MULTICAST_IFACE_ADDR={host_multicast_ip}",
            "-e", f"VNC_PORT={vnc_port}",
            "-e", f"NOVNC_PORT={novnc_port}",
            "-e", f"CAPTURE_AUDIO={'true' if capture_audio else 'false'}",
        ] + encoding_env + [CONTAINER_IMAGE]

        logger.info("Starting browser container: %s", " ".join(podman_cmd))
        rc, container_id, stderr = await self._run_cmd(podman_cmd)

        if rc != 0:
            # Container failed to start — release the display number and report the error
            self._release_display(display_num)
            raise ValueError(f"Container failed to start: {stderr}")

        # Use the short container ID (first 12 chars) for display/logging
        container_id = container_id[:12]
        managed = ManagedBrowser(stream_id, display_num, container_id)
        self._active[stream_id] = managed

        # Persist port assignments to DB so the API can serve them to the frontend
        db: Session = self._db_factory()
        try:
            browser = db.query(BrowserSource).filter(
                BrowserSource.stream_id == stream_id
            ).first()
            if browser:
                browser.display_number = display_num
                browser.vnc_port = vnc_port
                browser.novnc_port = novnc_port
            db.commit()
        finally:
            db.close()

        # Start a background task to detect unexpected container exits
        asyncio.create_task(self._watch_container(managed))

        logger.info(
            "Browser source started: stream=%d container=%s display=:%d vnc=%d novnc=%d",
            stream_id, container_id, display_num, vnc_port, novnc_port,
        )

        return {
            "container_id": container_id,
            "display": f":{display_num}",
            "vnc_port": vnc_port,
            "novnc_port": novnc_port,
        }

    async def start_presentation(self, stream_id: int, presentation_file_path: str,
                                  capture_audio: bool, multicast_address: str,
                                  multicast_port: int) -> dict:
        """
        Launch a Podman container for presentation capture and multicast output.

        Similar to start_browser(), but sets SOURCE_MODE=presentation and mounts
        the slideshow file (PPTX/ODP/PDF) into the container. The entrypoint.sh
        launches LibreOffice Impress with --show instead of Firefox.

        The presentation file is bind-mounted read-only at /tmp/presentation inside
        the container. LibreOffice opens it in slideshow mode on the Xvfb display.

        Args:
            stream_id: Database ID of the stream
            presentation_file_path: Absolute path to the slideshow file on the host
            capture_audio: Whether to enable PulseAudio capture (for embedded audio)
            multicast_address: Destination multicast group (e.g. "239.1.1.1")
            multicast_port: Destination UDP port

        Returns:
            Dict with container_id, display, vnc_port, novnc_port

        Raises:
            ValueError: If the file doesn't exist, image is unavailable, or container fails
        """
        # Validate the presentation file exists on the host
        if not os.path.isfile(presentation_file_path):
            raise ValueError(f"Presentation file not found: {presentation_file_path}")

        # Stop any existing container for this stream
        if stream_id in self._active:
            await self.stop_browser(stream_id)

        if not await self.ensure_image_built():
            raise ValueError("Capture source container image not available. "
                             "Check logs for build errors.")

        display_num = self._allocate_display()
        vnc_port = VNC_PORT_BASE + display_num
        novnc_port = NOVNC_PORT_BASE + display_num
        container_name = f"{CONTAINER_NAME_PREFIX}{stream_id}"

        # Remove any stale container with the same name
        await self._run_cmd(["sudo", "podman", "rm", "-f", container_name])

        host_multicast_ip = _detect_host_multicast_ip()
        if host_multicast_ip:
            logger.info("Multicast interface binding: localaddr=%s", host_multicast_ip)
        else:
            logger.warning("Could not detect host IP for multicast binding — "
                           "ffmpeg will rely on kernel routing table")

        # Read per-stream encoding profile from the database
        db: Session = self._db_factory()
        try:
            stream = db.query(Stream).filter(Stream.id == stream_id).first()
            if not stream:
                self._release_display(display_num)
                raise ValueError(f"Stream {stream_id} not found")
            encoding_env = self._get_container_encoding_env(stream)
        finally:
            db.close()

        # Determine the file extension so LibreOffice gets the correct filename
        # inside the container (it uses the extension to determine the file type)
        file_ext = os.path.splitext(presentation_file_path)[1]
        container_file = f"/tmp/presentation{file_ext}"

        podman_cmd = [
            "sudo", "podman", "run",
            "--detach",
            "--name", container_name,
            "--network=host",
            "--shm-size=512m",
            # Bind-mount the presentation file read-only into the container
            "-v", f"{presentation_file_path}:{container_file}:ro",
            "-e", f"DISPLAY_NUM={display_num}",
            # SOURCE_MODE tells entrypoint.sh to launch LibreOffice instead of Firefox
            "-e", "SOURCE_MODE=presentation",
            "-e", f"PRESENTATION_FILE={container_file}",
            "-e", f"MULTICAST_ADDR={multicast_address}",
            "-e", f"MULTICAST_PORT={multicast_port}",
            "-e", f"MULTICAST_TTL={config.MULTICAST_TTL}",
            "-e", f"MULTICAST_IFACE_ADDR={host_multicast_ip}",
            "-e", f"VNC_PORT={vnc_port}",
            "-e", f"NOVNC_PORT={novnc_port}",
            "-e", f"CAPTURE_AUDIO={'true' if capture_audio else 'false'}",
        ] + encoding_env + [CONTAINER_IMAGE]

        logger.info("Starting presentation container: %s", " ".join(podman_cmd))
        rc, container_id, stderr = await self._run_cmd(podman_cmd)

        if rc != 0:
            self._release_display(display_num)
            raise ValueError(f"Container failed to start: {stderr}")

        container_id = container_id[:12]
        managed = ManagedBrowser(stream_id, display_num, container_id)
        self._active[stream_id] = managed

        # Persist port assignments to DB via the stream's browser_source record
        # (presentation streams reuse the BrowserSource table for port tracking)
        db: Session = self._db_factory()
        try:
            browser = db.query(BrowserSource).filter(
                BrowserSource.stream_id == stream_id
            ).first()
            if browser:
                browser.display_number = display_num
                browser.vnc_port = vnc_port
                browser.novnc_port = novnc_port
            db.commit()
        finally:
            db.close()

        asyncio.create_task(self._watch_container(managed))

        logger.info(
            "Presentation source started: stream=%d container=%s display=:%d vnc=%d novnc=%d file=%s",
            stream_id, container_id, display_num, vnc_port, novnc_port, presentation_file_path,
        )

        return {
            "container_id": container_id,
            "display": f":{display_num}",
            "vnc_port": vnc_port,
            "novnc_port": novnc_port,
        }

    async def send_key(self, stream_id: int, key: str) -> bool:
        """
        Send a keyboard event to a running container via xdotool.

        Used to control LibreOffice Impress slideshow navigation from the API.
        The key name must be a valid X11 keysym (e.g. Right, Left, Home, End, Escape).

        Args:
            stream_id: Database ID of the stream
            key: X11 keysym name to send (e.g. "Right", "Left", "Home", "End", "Escape")

        Returns:
            True if the key was sent successfully, False otherwise
        """
        managed = self._active.get(stream_id)
        if not managed:
            logger.warning("send_key called for inactive stream %d", stream_id)
            return False

        # Whitelist of allowed key names to prevent command injection
        allowed_keys = {"Right", "Left", "Home", "End", "Escape", "space", "Return",
                        "Page_Up", "Page_Down", "Up", "Down"}
        if key not in allowed_keys:
            logger.warning("Rejected disallowed key '%s' for stream %d", key, stream_id)
            return False

        # xdotool needs the DISPLAY variable to target the correct Xvfb instance
        display = f":{managed.display_number}"
        rc, stdout, stderr = await self._run_cmd([
            "sudo", "podman", "exec",
            "-e", f"DISPLAY={display}",
            managed.container_name,
            "xdotool", "key", key,
        ])

        if rc != 0:
            logger.warning("xdotool key send failed for stream %d: %s", stream_id, stderr)
            return False

        logger.debug("Sent key '%s' to stream %d container %s",
                     key, stream_id, managed.container_name)
        return True

    async def stop_browser(self, stream_id: int):
        """
        Stop and remove the Podman container for a browser source.

        Sends SIGTERM via `podman stop` (with 10s grace period for entrypoint.sh to
        clean up child processes), then force-removes the container.

        Args:
            stream_id: Database ID of the stream whose browser source to stop
        """
        managed = self._active.get(stream_id)
        if not managed:
            return

        # Signal the watcher to not treat this exit as an error
        managed.should_stop = True
        container_name = managed.container_name

        # podman stop sends SIGTERM to the entrypoint, which should trap it and kill children
        # -t 10 gives it 10 seconds before podman sends SIGKILL
        logger.info("Stopping browser container %s", container_name)
        await self._run_cmd(["sudo", "podman", "stop", "-t", "10", container_name])
        # Force remove to clean up the container filesystem even if stop was unclean
        await self._run_cmd(["sudo", "podman", "rm", "-f", container_name])

        self._release_display(managed.display_number)
        del self._active[stream_id]

        # Clear port assignments in DB since the container is no longer listening
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

        logger.info("Browser source stopped: stream=%d", stream_id)

    async def _watch_container(self, managed: ManagedBrowser):
        """
        Poll the container's running state and handle unexpected exits.

        Checks every 5 seconds via `podman inspect`. If the container has exited
        and should_stop is False (not a user-initiated stop), marks the stream
        as ERROR in the database so the frontend can display the failure.

        Args:
            managed: The ManagedBrowser being watched
        """
        try:
            while not managed.should_stop:
                await asyncio.sleep(5)
                # Ask Podman if the container is still running
                rc, stdout, _ = await self._run_cmd([
                    "sudo", "podman", "inspect", "--format", "{{.State.Running}}",
                    managed.container_name,
                ])
                if rc != 0 or stdout.lower() != "true":
                    if not managed.should_stop:
                        logger.warning("Browser container %s exited unexpectedly",
                                       managed.container_name)
                        # Clean up the tracking state
                        if managed.stream_id in self._active:
                            self._release_display(managed.display_number)
                            del self._active[managed.stream_id]
                        # Mark stream as errored in DB so the UI reflects the failure
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
            # Watcher cancelled during shutdown — expected, not an error
            pass
        except Exception:
            logger.exception("Error watching container for stream %d",
                             managed.stream_id)

    def get_browser_pids(self, stream_id: int) -> list:
        """
        Get the container's main PID for resource monitoring via psutil.

        Uses synchronous subprocess (not async) because this is called from
        the monitoring endpoint which needs immediate results. The PID returned
        is the container's init process; psutil can then walk its children to
        find ffmpeg, Firefox, etc.

        Args:
            stream_id: Database ID of the stream

        Returns:
            List containing the container's main PID, or empty list if unavailable
        """
        managed = self._active.get(stream_id)
        if not managed:
            return []
        # Synchronous subprocess call — this method is called from sync monitoring code
        try:
            result = subprocess.run(
                ["sudo", "podman", "inspect", "--format", "{{.State.Pid}}",
                 managed.container_name],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                pid = int(result.stdout.strip())
                if pid > 0:
                    return [pid]
        except subprocess.TimeoutExpired:
            logger.warning("Timed out getting PID for container %s", managed.container_name)
        except (ValueError, OSError) as exc:
            logger.debug("Failed to get container PID for stream %d: %s", stream_id, exc)
        return []

    def get_status(self, stream_id: int) -> dict:
        """
        Get the runtime status of a browser source for the API.

        Args:
            stream_id: Database ID of the stream

        Returns:
            Dict with active state, container info, display/port assignments, and PIDs
        """
        managed = self._active.get(stream_id)
        if not managed:
            return {"active": False}
        return {
            "active": True,
            "container_id": managed.container_id,
            "display": f":{managed.display_number}",
            "vnc_port": managed.vnc_port,
            "novnc_port": managed.novnc_port,
            "pids": self.get_browser_pids(stream_id),
        }

    async def stop_all(self):
        """
        Kill all browser containers during application shutdown.

        Unlike stop_browser(), this intentionally does NOT update DB status.
        Streams stay marked as RUNNING so restore_sessions() can restart them
        when the service comes back up. Only the containers and local state
        are cleaned up.
        """
        for sid in list(self._active.keys()):
            managed = self._active[sid]
            managed.should_stop = True
            container_name = managed.container_name
            await self._run_cmd(["sudo", "podman", "stop", "-t", "10", container_name])
            await self._run_cmd(["sudo", "podman", "rm", "-f", container_name])
            self._release_display(managed.display_number)
            logger.info("Browser container %s killed (shutdown)", container_name)
        self._active.clear()

    async def restore_sessions(self):
        """
        Restore container-based sources that were marked as RUNNING before a service restart.

        On startup, queries the DB for any streams with source_type BROWSER or
        PRESENTATION and status=RUNNING, then attempts to relaunch their containers.
        This handles the case where the systemd service was restarted but the streams
        should continue. If a container fails to start, the stream is marked as ERROR.
        """
        db: Session = self._db_factory()
        try:
            # Find all container-based streams that should be running
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
                        # Presentation streams need the file path from the linked presentation
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
