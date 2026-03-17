"""
Stream Manager — controls ffmpeg multicast playout subprocesses.
Generates concat lists, launches ffmpeg for re-mux to UDP multicast,
monitors processes, and handles loop/restart behavior.
"""

import asyncio
import logging
import os
import signal
from typing import Dict, Optional
from sqlalchemy.orm import Session
from backend import config
from backend.models import Stream, StreamStatus, PlaybackMode, AssetStatus

logger = logging.getLogger("stream_manager")


class ManagedStream:
    """State holder for a single running ffmpeg playout process."""
    def __init__(self, stream_id, process, concat_file_path, loop=True):
        self.stream_id = stream_id
        self.process = process
        self.concat_file_path = concat_file_path
        self.loop = loop
        self.restart_task: Optional[asyncio.Task] = None
        self.should_stop = False


class StreamManager:
    """
    Manages all active multicast stream ffmpeg processes.
    V1: single stream.  Architecture supports multiple via MAX_CONCURRENT_STREAMS.
    """

    def __init__(self, db_session_factory):
        self._active: Dict[int, ManagedStream] = {}
        self._db_factory = db_session_factory

    @property
    def active_stream_count(self):
        return len(self._active)

    def is_stream_active(self, stream_id):
        return stream_id in self._active

    def _generate_concat_file(self, stream: Stream) -> str:
        """Write an ffmpeg concat demuxer file from the stream's playlist."""
        path = str(config.CONCAT_DIR / f"stream_{stream.id}_playlist.txt")
        with open(path, "w") as f:
            for item in sorted(stream.items, key=lambda i: i.position):
                if item.asset.status == AssetStatus.READY:
                    safe = item.asset.file_path.replace("'", "'\\''")
                    f.write(f"file '{safe}'\n")
        return path

    def _build_playout_cmd(self, concat_path: str, stream: Stream) -> list:
        """Build ffmpeg re-mux command: concat -> mpegts -> udp multicast."""
        url = (f"udp://{stream.multicast_address}:{stream.multicast_port}"
               f"?pkt_size=1316&ttl={config.MULTICAST_TTL}")
        cmd = [config.FFMPEG_PATH, "-y", "-re"]
        if stream.playback_mode == PlaybackMode.LOOP:
            cmd += ["-stream_loop", "-1"]
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
        if (self.active_stream_count >= config.MAX_CONCURRENT_STREAMS
                and stream_id not in self._active):
            raise ValueError(
                f"Max concurrent streams ({config.MAX_CONCURRENT_STREAMS}) reached")

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

            concat_path = self._generate_concat_file(stream)
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
            stream.ffmpeg_pid = proc.pid
            db.commit()

            managed.restart_task = asyncio.create_task(self._watch(managed))
            logger.info("Stream %d running PID %d -> udp://%s:%d",
                         stream_id, proc.pid,
                         stream.multicast_address, stream.multicast_port)
        finally:
            db.close()

    async def stop_stream(self, stream_id: int) -> None:
        managed = self._active.get(stream_id)
        if not managed:
            return

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

        del self._active[stream_id]

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
        """Watch ffmpeg process; restart on crash if loop mode."""
        try:
            rc = await managed.process.wait()
            logger.info("Stream %d ffmpeg exited code %d", managed.stream_id, rc)
            if managed.should_stop:
                return
            if managed.loop and rc != 0:
                logger.warning("Stream %d crashed, restarting...", managed.stream_id)
                if managed.stream_id in self._active:
                    del self._active[managed.stream_id]
                await asyncio.sleep(2)
                try:
                    await self.start_stream(managed.stream_id)
                except Exception as e:
                    logger.error("Restart failed: %s", e)
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
            pass

    async def stop_all(self) -> None:
        for sid in list(self._active.keys()):
            await self.stop_stream(sid)

    def get_status(self, stream_id: int) -> dict:
        m = self._active.get(stream_id)
        if not m:
            return {"active": False, "pid": None}
        return {"active": True, "pid": m.process.pid, "returncode": m.process.returncode}
