"""
Stream Manager — controls ffmpeg multicast playout subprocesses for playlist-type streams.

This service is responsible for the entire lifecycle of multicast playout:
  1. Generating ffmpeg concat-demuxer playlist files from the stream's ordered assets
  2. Launching ffmpeg subprocesses that re-mux (not re-encode) pre-transcoded assets
     into MPEG-TS and push them out as UDP multicast
  3. Monitoring those processes and auto-restarting on crash (in loop mode)
  4. Tracking stream status and PID in the database for the frontend and monitoring

Assets are pre-transcoded into multiple renditions (resolution/codec combinations) by the
transcode ladder. At playout time, the concat file is built using the rendition that
matches the stream's encoding profile. The playout ffmpeg uses -c copy (stream copy)
which is extremely lightweight — it's just remuxing, not encoding.

The multicast output uses MPEG-TS container format with 1316-byte UDP packets (the
standard MPEG-TS-over-UDP packet size: 7 x 188-byte TS packets).
"""

import asyncio
import logging
import os
import signal
from typing import Dict, Optional
from sqlalchemy.orm import Session
from backend import config
from backend.models import (
    Stream, StreamStatus, StreamSourceType, PlaybackMode,
    AssetStatus, AssetRendition, RenditionStatus,
)
from backend.services.browser_manager import _detect_host_multicast_ip

logger = logging.getLogger("stream_manager")


class ManagedStream:
    """
    State holder for a single running ffmpeg playout process.

    Tracks the asyncio subprocess, the temporary concat file on disk,
    and control flags for the lifecycle watcher.
    """
    def __init__(self, stream_id, process, concat_file_path, loop=True):
        self.stream_id = stream_id
        self.process = process
        self.concat_file_path = concat_file_path
        self.loop = loop
        # Background task that watches for process exit and handles restart
        self.restart_task: Optional[asyncio.Task] = None
        # Set to True when stop_stream() is called to prevent the watcher from restarting
        self.should_stop = False


class StreamManager:
    """
    Manages all active multicast stream ffmpeg processes.

    Maintains a dict of stream_id -> ManagedStream for all currently-running
    playout processes. Enforces MAX_CONCURRENT_STREAMS limit from config.
    """

    def __init__(self, db_session_factory):
        self._active: Dict[int, ManagedStream] = {}
        self._db_factory = db_session_factory

    @property
    def active_stream_count(self):
        """Number of currently-running playout processes."""
        return len(self._active)

    def is_stream_active(self, stream_id):
        """Check if a specific stream has a running ffmpeg process."""
        return stream_id in self._active

    def _select_rendition_path(self, asset, target_resolution: str,
                               target_codec: str, target_framerate: int,
                               db: Session) -> str | None:
        """Find the best rendition file for a given stream encoding profile.

        Selection priority:
          1. Exact match on resolution + codec + framerate
          2. Exact resolution + codec, any framerate
          3. Same resolution, any codec/framerate
          4. Fall back to asset.file_path (legacy single-file or first ready rendition)

        Args:
            asset: Asset ORM object
            target_resolution: Stream's configured resolution (e.g. "1920x1080")
            target_codec: Stream's configured codec ("h264" or "h265")
            target_framerate: Stream's configured framerate (30 or 60)
            db: Active database session

        Returns:
            Filesystem path to the best matching rendition, or None if nothing usable
        """
        renditions = db.query(AssetRendition).filter(
            AssetRendition.asset_id == asset.id,
            AssetRendition.status == RenditionStatus.READY,
        ).all()

        if not renditions:
            # No renditions yet (legacy asset) — use the old file_path
            return asset.file_path

        # Priority 1: exact match on resolution + codec + framerate
        for r in renditions:
            if (r.resolution == target_resolution and r.codec == target_codec
                    and r.framerate == target_framerate):
                return r.file_path

        # Priority 2: resolution + codec match, any framerate
        for r in renditions:
            if r.resolution == target_resolution and r.codec == target_codec:
                return r.file_path

        # Priority 3: same resolution, different codec
        for r in renditions:
            if r.resolution == target_resolution:
                return r.file_path

        # Priority 4: any ready rendition
        return asset.file_path

    # Number of times to repeat the playlist in the concat file for loop mode.
    # Avoids -stream_loop which causes PTS/DTS discontinuities at the loop
    # boundary that hardware decoders (VITEC EP6) can't handle. When the
    # concat file is exhausted, the watcher task auto-restarts the stream
    # (regenerating a fresh concat file), so this effectively loops forever
    # with clean timestamp continuity within each ffmpeg invocation.
    # 500 repeats of a typical playlist = many hours of continuous playout.
    LOOP_REPEATS = 500

    def _generate_concat_file(self, stream: Stream, db: Session) -> str:
        """
        Write an ffmpeg concat-demuxer playlist file from the stream's ordered items.

        Selects the rendition matching the stream's encoding profile for each asset.
        The concat demuxer format is simply:
            file '/path/to/asset_1.mp4'
            file '/path/to/asset_2.mp4'
            ...

        In loop mode, the playlist entries are repeated LOOP_REPEATS times
        in the concat file itself (instead of using -stream_loop). This
        ensures the concat demuxer handles every file-to-file transition
        natively with continuous timestamps — no splice point where ffmpeg
        has to wrap PTS/DTS back to zero.

        Only includes assets with READY status and a usable rendition file.
        Single quotes in file paths are escaped for the concat demuxer syntax.

        Args:
            stream: Stream ORM object with loaded .items relationship
            db: Active database session for rendition lookups

        Returns:
            Filesystem path to the generated concat playlist file
        """
        path = str(config.CONCAT_DIR / f"stream_{stream.id}_playlist.txt")

        # Build the single-pass entry list
        entries = []
        for item in sorted(stream.items, key=lambda i: i.position):
            if item.asset.status != AssetStatus.READY:
                continue
            rendition_path = self._select_rendition_path(
                item.asset, stream.resolution, stream.codec,
                stream.framerate, db)
            if rendition_path:
                safe = rendition_path.replace("'", "'\\''")
                entries.append(f"file '{safe}'\n")

        # In loop mode, repeat the entries many times so a single ffmpeg
        # invocation plays for hours without needing -stream_loop.
        repeat_count = self.LOOP_REPEATS if stream.playback_mode == PlaybackMode.LOOP else 1

        with open(path, "w") as f:
            for _ in range(repeat_count):
                for entry in entries:
                    f.write(entry)
        return path

    def _build_playout_cmd(self, concat_path: str, stream: Stream) -> list:
        """
        Build the ffmpeg command for multicast playout via concat demuxer.

        Key flags:
          -re: Read input at native framerate (realtime pacing), essential for live playout.
               Without this, ffmpeg would blast through the file as fast as possible.
          -stream_loop -1: Infinite loop (only added in LOOP playback mode)
          -c copy: Stream copy (no re-encoding) — possible because all assets are
                   pre-transcoded to the same profile
          -f mpegts: Output as MPEG Transport Stream, the standard for UDP multicast
          pkt_size=1316: 7 x 188-byte TS packets per UDP datagram (industry standard)
          ttl: Multicast Time-To-Live, controls how many router hops the packets can traverse

        Args:
            concat_path: Path to the generated concat-demuxer playlist file
            stream: Stream ORM object with multicast address/port configuration

        Returns:
            Complete ffmpeg command as a list of strings
        """
        url = (f"udp://{stream.multicast_address}:{stream.multicast_port}"
               f"?pkt_size=1316&ttl={config.MULTICAST_TTL}")
        # Bind to the host's primary NIC so multicast packets go out the right
        # interface instead of potentially hitting loopback
        host_ip = _detect_host_multicast_ip()
        if host_ip:
            url += f"&localaddr={host_ip}"
        # No -stream_loop flag. Looping is handled by repeating the playlist
        # entries in the concat file (see _generate_concat_file). This avoids
        # the PTS/DTS timestamp discontinuity that -stream_loop creates at
        # the splice point — hardware decoders (VITEC EP6) macroblock when
        # they see a backwards DTS jump. With repeated concat entries, every
        # file transition is handled natively by the concat demuxer with
        # continuous, monotonic timestamps.
        cmd = [config.FFMPEG_PATH, "-y", "-re"]
        cmd += [
            "-f", "concat", "-safe", "0", "-i", concat_path,
            "-c:v", "copy", "-c:a", "copy",
            "-f", "mpegts",
            "-mpegts_transport_stream_id", "1",
            "-metadata", f"service_name={stream.name}",
            url,
        ]
        return cmd

    async def start_stream(self, stream_id: int) -> None:
        """
        Start multicast playout for a stream.

        Validates the stream has ready assets, generates the concat file,
        launches ffmpeg, and starts a background watcher task for crash recovery.

        Args:
            stream_id: Database ID of the Stream to start

        Raises:
            ValueError: If max concurrent streams reached, stream not found, or no ready assets
        """
        if (self.active_stream_count >= config.MAX_CONCURRENT_STREAMS
                and stream_id not in self._active):
            raise ValueError(
                f"Max concurrent streams ({config.MAX_CONCURRENT_STREAMS}) reached")

        # If the stream is already running, stop it first (handles playlist changes)
        if stream_id in self._active:
            await self.stop_stream(stream_id)

        db: Session = self._db_factory()
        try:
            stream = db.query(Stream).filter(Stream.id == stream_id).first()
            if not stream:
                raise ValueError(f"Stream {stream_id} not found")
            if not stream.items:
                raise ValueError("Stream has no playlist items")

            ready = [i for i in stream.items if i.asset.status == AssetStatus.READY]
            if not ready:
                raise ValueError("No ready assets in the playlist")

            # Runtime conflict check: verify no other running stream is using the
            # same multicast address:port. The DB-level check at update time can
            # miss races (e.g., address changed but not yet restarted).
            conflict = db.query(Stream).filter(
                Stream.multicast_address == stream.multicast_address,
                Stream.multicast_port == stream.multicast_port,
                Stream.id != stream_id,
                Stream.status == StreamStatus.RUNNING,
            ).first()
            if conflict:
                raise ValueError(
                    f"Multicast {stream.multicast_address}:{stream.multicast_port} "
                    f"is already in use by running stream '{conflict.name}' (id={conflict.id})"
                )

            concat_path = self._generate_concat_file(stream, db)
            cmd = self._build_playout_cmd(concat_path, stream)
            logger.info("Starting stream %d: %s", stream_id, " ".join(cmd))

            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

            managed = ManagedStream(
                stream_id=stream_id, process=proc,
                concat_file_path=concat_path,
                loop=(stream.playback_mode == PlaybackMode.LOOP))
            self._active[stream_id] = managed

            stream.status = StreamStatus.RUNNING
            # Store PID so the monitoring service can track resource usage
            stream.ffmpeg_pid = proc.pid
            db.commit()

            # Start background task that waits for process exit and handles restart/cleanup
            managed.restart_task = asyncio.create_task(self._watch(managed))
            logger.info("Stream %d running PID %d -> udp://%s:%d",
                         stream_id, proc.pid,
                         stream.multicast_address, stream.multicast_port)
        finally:
            db.close()

    async def stop_stream(self, stream_id: int) -> None:
        """
        Stop a running stream's ffmpeg process and clean up.

        Sends SIGTERM first (allows ffmpeg to flush buffers), then SIGKILL
        after 5 seconds if it hasn't exited. Removes the concat playlist file
        and updates DB status.

        Args:
            stream_id: Database ID of the Stream to stop
        """
        managed = self._active.get(stream_id)
        if not managed:
            return

        # Signal the watcher task to not attempt restart
        managed.should_stop = True
        if managed.restart_task and not managed.restart_task.done():
            managed.restart_task.cancel()

        try:
            # SIGTERM lets ffmpeg flush output buffers gracefully
            managed.process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(managed.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                # ffmpeg didn't exit in 5 seconds — force kill
                managed.process.kill()
                await managed.process.wait()
        except ProcessLookupError:
            # Process already exited (race condition with watcher) — safe to ignore
            pass

        # Clean up the temporary concat playlist file
        try:
            os.remove(managed.concat_file_path)
        except OSError:
            pass

        del self._active[stream_id]

        # Update DB to reflect stopped state
        db: Session = self._db_factory()
        try:
            stream = db.query(Stream).filter(Stream.id == stream_id).first()
            if stream:
                stream.status = StreamStatus.STOPPED
                stream.ffmpeg_pid = None
                db.commit()
        finally:
            db.close()
        logger.info("Stream %d stopped", stream_id)

    async def _watch(self, managed: ManagedStream) -> None:
        """
        Background task: watch an ffmpeg process and handle exit.

        Behavior depends on exit conditions:
          - If should_stop is True: user requested stop, do nothing (stop_stream handles cleanup)
          - If loop mode: restart regardless of exit code. rc=0 means the concat
            file was exhausted (normal — we use repeated concat entries instead of
            -stream_loop), rc!=0 means crash. Either way, restart with a fresh
            concat file for seamless continuous playout.
          - If non-loop mode (play once): natural completion, mark stream as stopped

        The 2-second delay before restart prevents rapid restart loops if ffmpeg is
        crashing immediately (e.g., due to a bad concat file).

        Args:
            managed: The ManagedStream being watched
        """
        try:
            rc = await managed.process.wait()
            logger.info("Stream %d ffmpeg exited code %d", managed.stream_id, rc)
            if managed.should_stop:
                # User-initiated stop — stop_stream() handles cleanup
                return
            if managed.loop:
                # Loop mode — restart regardless of exit code. rc=0 means the
                # concat file was exhausted (expected after LOOP_REPEATS cycles),
                # rc!=0 means crash. Either way, regenerate concat and restart.
                if rc != 0:
                    logger.warning("Stream %d crashed (rc=%d), restarting...",
                                   managed.stream_id, rc)
                else:
                    logger.info("Stream %d concat exhausted, restarting loop...",
                                managed.stream_id)
                if managed.stream_id in self._active:
                    del self._active[managed.stream_id]
                # Brief delay to avoid tight restart loops on persistent failures
                await asyncio.sleep(2)
                try:
                    await self.start_stream(managed.stream_id)
                except Exception as e:
                    logger.error("Restart failed: %s", e)
                    # Mark stream as errored so the UI shows the failure
                    db = self._db_factory()
                    try:
                        s = db.query(Stream).filter(Stream.id == managed.stream_id).first()
                        if s:
                            s.status = StreamStatus.ERROR
                            s.ffmpeg_pid = None
                            db.commit()
                    finally:
                        db.close()
            elif not managed.loop:
                # Play-once mode: natural end of playlist, mark as stopped
                if managed.stream_id in self._active:
                    del self._active[managed.stream_id]
                db = self._db_factory()
                try:
                    s = db.query(Stream).filter(Stream.id == managed.stream_id).first()
                    if s:
                        s.status = StreamStatus.STOPPED
                        s.ffmpeg_pid = None
                        db.commit()
                finally:
                    db.close()
        except asyncio.CancelledError:
            # Watcher was cancelled by stop_stream() — expected, not an error
            pass

    async def restore_sessions(self) -> None:
        """
        Re-start playlist streams that were running before a server restart.

        Queries the DB for streams with status=RUNNING and source_type=PLAYLIST,
        then attempts to start each one. Streams that fail to start are marked ERROR.
        Called once during application startup from the lifespan handler.
        """
        db: Session = self._db_factory()
        try:
            stale_streams = db.query(Stream).filter(
                Stream.status == StreamStatus.RUNNING,
                Stream.source_type == StreamSourceType.PLAYLIST,
            ).all()
            stream_ids = [s.id for s in stale_streams]
        finally:
            db.close()

        if not stream_ids:
            return

        logger.info("Restoring %d playlist stream(s): %s", len(stream_ids), stream_ids)
        for sid in stream_ids:
            try:
                await self.start_stream(sid)
                logger.info("Restored playlist stream %d", sid)
            except Exception as exc:
                logger.error("Failed to restore stream %d: %s", sid, exc)
                db = self._db_factory()
                try:
                    s = db.query(Stream).filter(Stream.id == sid).first()
                    if s:
                        s.status = StreamStatus.ERROR
                        s.ffmpeg_pid = None
                        db.commit()
                finally:
                    db.close()

    async def stop_all(self) -> None:
        """
        Kill all active ffmpeg processes during application shutdown.

        Unlike stop_stream(), this intentionally does NOT update DB status.
        Streams stay marked as RUNNING so restore_sessions() can restart them
        when the service comes back up. Only the processes and local state
        are cleaned up.
        """
        for sid in list(self._active.keys()):
            managed = self._active[sid]
            managed.should_stop = True
            if managed.restart_task and not managed.restart_task.done():
                managed.restart_task.cancel()
            try:
                managed.process.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(managed.process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    managed.process.kill()
                    await managed.process.wait()
            except ProcessLookupError:
                pass
            try:
                os.remove(managed.concat_file_path)
            except OSError:
                pass
            logger.info("Stream %d process killed (shutdown)", sid)
        self._active.clear()

    def get_status(self, stream_id: int) -> dict:
        """
        Get the runtime status of a stream for the API.

        Args:
            stream_id: Database ID of the stream

        Returns:
            Dict with 'active' bool, 'pid' (or None), and 'returncode' (None if still running)
        """
        m = self._active.get(stream_id)
        if not m:
            return {"active": False, "pid": None}
        return {"active": True, "pid": m.process.pid, "returncode": m.process.returncode}
