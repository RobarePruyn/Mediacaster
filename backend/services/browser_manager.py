"""
Browser Source Manager — Podman container-based virtual browser capture.

Each browser source runs as an isolated Podman container based on AlmaLinux 9,
providing the X11 stack (Xvfb, x11vnc, xdotool) that AL10 dropped.

Container per source provides:
    - Xvfb virtual display at the configured resolution
    - Firefox in kiosk mode pointed at the configured URL
    - x11vnc for interactive VNC control
    - websockify/noVNC for web-based interaction (embedded in the UI)
    - ffmpeg x11grab → MPEG-TS UDP multicast output
    - Optional PulseAudio audio capture from the browser

Uses --network=host for direct multicast output capability.
"""

import asyncio
import logging
import json
from typing import Dict, Optional
from sqlalchemy.orm import Session
from backend import config
from backend.models import BrowserSource, Stream, StreamStatus

logger = logging.getLogger("browser_manager")

CONTAINER_IMAGE = "mcs-browser-source:latest"
CONTAINER_NAME_PREFIX = "mcs-browser-"

# Port allocation ranges — each container gets a unique pair
DISPLAY_BASE = 50
VNC_PORT_BASE = 5950
NOVNC_PORT_BASE = 6080


class ManagedBrowser:
    """Tracks a running browser source container."""
    def __init__(self, stream_id: int, display_number: int, container_id: str):
        self.stream_id = stream_id
        self.display_number = display_number
        self.container_id = container_id
        self.should_stop = False

    @property
    def container_name(self):
        return f"{CONTAINER_NAME_PREFIX}{self.stream_id}"

    @property
    def vnc_port(self):
        return VNC_PORT_BASE + self.display_number

    @property
    def novnc_port(self):
        return NOVNC_PORT_BASE + self.display_number


class BrowserManager:
    """Manages Podman containers for browser source capture."""

    def __init__(self, db_session_factory):
        self._active: Dict[int, ManagedBrowser] = {}
        self._db_factory = db_session_factory
        self._used_displays = set()

    def _allocate_display(self) -> int:
        display = DISPLAY_BASE
        while display in self._used_displays:
            display += 1
        self._used_displays.add(display)
        return display

    def _release_display(self, display: int):
        self._used_displays.discard(display)

    def is_active(self, stream_id: int) -> bool:
        return stream_id in self._active

    def get_novnc_port(self, stream_id: int) -> Optional[int]:
        managed = self._active.get(stream_id)
        return managed.novnc_port if managed else None

    async def _run_cmd(self, cmd: list) -> tuple:
        """Run a shell command async, return (returncode, stdout, stderr)."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode, stdout.decode().strip(), stderr.decode().strip()

    async def ensure_image_built(self) -> bool:
        """Build the container image if it doesn't already exist."""
        # Check if image exists
        rc, stdout, _ = await self._run_cmd([
            "sudo", "podman", "image", "exists", CONTAINER_IMAGE
        ])
        if rc == 0:
            logger.info("Container image %s already exists", CONTAINER_IMAGE)
            return True

        # Build the image from the Containerfile
        import os
        container_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "container"
        )
        # Also check the installed location
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

    async def start_browser(self, stream_id: int, url: str, capture_audio: bool,
                            multicast_address: str, multicast_port: int) -> dict:
        """Launch a Podman container for browser source capture."""

        if stream_id in self._active:
            await self.stop_browser(stream_id)

        # Ensure the container image is built
        if not await self.ensure_image_built():
            raise ValueError("Browser source container image not available. "
                             "Check logs for build errors.")

        display_num = self._allocate_display()
        vnc_port = VNC_PORT_BASE + display_num
        novnc_port = NOVNC_PORT_BASE + display_num
        container_name = f"{CONTAINER_NAME_PREFIX}{stream_id}"
        resolution = config.TRANSCODE_RESOLUTION
        framerate = config.TRANSCODE_FRAMERATE

        # Remove any stale container with the same name
        await self._run_cmd(["sudo", "podman", "rm", "-f", container_name])

        # Build the podman run command
        # --network=host is required for multicast UDP output
        podman_cmd = [
            "sudo", "podman", "run",
            "--detach",
            "--name", container_name,
            "--network=host",              # Required for multicast output
            "--shm-size=512m",             # Firefox needs shared memory for rendering
            "-e", f"DISPLAY_NUM={display_num}",
            "-e", f"RESOLUTION={resolution}",
            "-e", f"FRAMERATE={framerate}",
            "-e", f"URL={url}",
            "-e", f"MULTICAST_ADDR={multicast_address}",
            "-e", f"MULTICAST_PORT={multicast_port}",
            "-e", f"MULTICAST_TTL={config.MULTICAST_TTL}",
            "-e", f"VNC_PORT={vnc_port}",
            "-e", f"NOVNC_PORT={novnc_port}",
            "-e", f"CAPTURE_AUDIO={'true' if capture_audio else 'false'}",
            "-e", f"VIDEO_BITRATE={config.TRANSCODE_VIDEO_BITRATE}",
            "-e", f"AUDIO_BITRATE={config.TRANSCODE_AUDIO_BITRATE}",
            CONTAINER_IMAGE,
        ]

        logger.info("Starting browser container: %s", " ".join(podman_cmd))
        rc, container_id, stderr = await self._run_cmd(podman_cmd)

        if rc != 0:
            self._release_display(display_num)
            raise ValueError(f"Container failed to start: {stderr}")

        container_id = container_id[:12]  # Short ID
        managed = ManagedBrowser(stream_id, display_num, container_id)
        self._active[stream_id] = managed

        # Update DB with port assignments
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

        # Start a background watcher to detect container exit
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

    async def stop_browser(self, stream_id: int):
        """Stop and remove the container for a browser source."""
        managed = self._active.get(stream_id)
        if not managed:
            return

        managed.should_stop = True
        container_name = managed.container_name

        # Stop the container (sends SIGTERM to entrypoint, which cleans up children)
        logger.info("Stopping browser container %s", container_name)
        await self._run_cmd(["sudo", "podman", "stop", "-t", "10", container_name])
        await self._run_cmd(["sudo", "podman", "rm", "-f", container_name])

        self._release_display(managed.display_number)
        del self._active[stream_id]

        # Update DB
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
        """Monitor a container and update state if it exits unexpectedly."""
        try:
            while not managed.should_stop:
                await asyncio.sleep(5)
                # Check if container is still running
                rc, stdout, _ = await self._run_cmd([
                    "sudo", "podman", "inspect", "--format", "{{.State.Running}}",
                    managed.container_name,
                ])
                if rc != 0 or stdout.lower() != "true":
                    if not managed.should_stop:
                        logger.warning("Browser container %s exited unexpectedly",
                                       managed.container_name)
                        # Clean up
                        if managed.stream_id in self._active:
                            self._release_display(managed.display_number)
                            del self._active[managed.stream_id]
                        # Update DB status
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
            logger.exception("Error watching container for stream %d",
                             managed.stream_id)

    def get_browser_pids(self, stream_id: int) -> list:
        """
        Get PIDs for resource monitoring.
        With containers, we get the main container PID from podman.
        """
        managed = self._active.get(stream_id)
        if not managed:
            return []
        # We'll get the container's main PID synchronously via subprocess
        import subprocess
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
        except Exception:
            pass
        return []

    def get_status(self, stream_id: int) -> dict:
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
        """Stop all active browser containers."""
        for sid in list(self._active.keys()):
            await self.stop_browser(sid)

    async def restore_sessions(self):
        """Restore browser sources that were running before a service restart."""
        db: Session = self._db_factory()
        try:
            from backend.models import StreamSourceType
            browser_streams = db.query(Stream).filter(
                Stream.source_type == StreamSourceType.BROWSER,
                Stream.status == StreamStatus.RUNNING,
            ).all()

            for stream in browser_streams:
                if stream.browser_source and stream.browser_source.url:
                    logger.info("Restoring browser source for stream %d: %s",
                                stream.id, stream.browser_source.url)
                    try:
                        await self.start_browser(
                            stream.id,
                            stream.browser_source.url,
                            stream.browser_source.capture_audio,
                            stream.multicast_address,
                            stream.multicast_port,
                        )
                        stream.status = StreamStatus.RUNNING
                    except Exception as exc:
                        logger.error("Failed to restore browser stream %d: %s",
                                     stream.id, exc)
                        stream.status = StreamStatus.ERROR
            db.commit()
        finally:
            db.close()
