"""
Transcoding service — normalizes uploaded media and generates a rendition ladder.

On upload, each asset is transcoded into multiple renditions at different
resolution/codec combinations (the "transcode ladder"). Playlist streams then
use -c copy from the rendition matching their output encoding profile.

The ladder is configured via the transcode_ladder server setting:
  {"720p_h264": true, "1080p_h264": true, "1080p_h265": false, "4k_h265": false}

Supports three asset types:
  - Video: re-encodes to target profile(s)
  - Image: converts to a looping video clip (black + image for configured duration)
  - Audio: generates black video frames + source audio

All renditions are 60fps for smooth motion. Streams at 30fps use frame
dropping at playout time.
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from sqlalchemy.orm import Session
from backend import config
from backend.models import Asset, AssetStatus, AssetType, AssetRendition, RenditionStatus
from backend.services.encoding_profiles import (
    get_transcode_ladder, get_effective_bitrate, RESOLUTION_720P,
    RESOLUTION_1080P, RESOLUTION_4K,
)

logger = logging.getLogger("transcoder")

# Limit concurrent transcodes to avoid saturating CPU/memory when multiple
# users upload simultaneously. Queued transcodes wait here until a slot opens.
_transcode_semaphore = asyncio.Semaphore(2)

# Default transcode ladder framerate — all renditions are 60fps for smooth motion.
# Streams configured at 30fps use frame dropping at playout time.
LADDER_FRAMERATE = 60

# Recognized file extensions by media type
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".ts", ".m2ts", ".mxf",
                    ".flv", ".wmv", ".webm", ".mpg", ".mpeg", ".m4v"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".aac", ".flac", ".ogg", ".opus", ".m4a", ".wma", ".aiff"}
GIF_EXTENSION = {".gif"}
ALL_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS | GIF_EXTENSION


def classify_upload(filename: str) -> AssetType:
    """Determine the asset type from a filename's extension."""
    ext = Path(filename).suffix.lower()
    if ext in VIDEO_EXTENSIONS:
        return AssetType.VIDEO
    elif ext in IMAGE_EXTENSIONS or ext in GIF_EXTENSION:
        return AssetType.IMAGE
    elif ext in AUDIO_EXTENSIONS:
        return AssetType.AUDIO
    raise ValueError(f"Unsupported file type: {ext}")


# ── Subprocess helpers ───────────────────────────────────────────────────────

async def _run_command(command: list) -> tuple:
    """Run a subprocess asynchronously and capture all output."""
    logger.info("Running: %s", " ".join(command))
    proc = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout_bytes, stderr_bytes = await proc.communicate()
    return proc.returncode, stdout_bytes.decode(), stderr_bytes.decode()


async def _run_with_progress(command: list, source_duration: float,
                              asset_id: int, db_session_factory,
                              rendition_id: int | None = None) -> tuple:
    """Run ffmpeg with real-time progress tracking.

    Parses -progress pipe:1 output to update the asset's (or rendition's)
    transcode_progress in the database.
    """
    logger.info("Running with progress: %s", " ".join(command))
    proc = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    last_progress = 0.0
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        decoded = line.decode().strip()

        if decoded.startswith("out_time_us="):
            try:
                elapsed = int(decoded.split("=")[1]) / 1_000_000
            except (ValueError, IndexError):
                continue
        elif decoded.startswith("out_time="):
            time_val = decoded.split("=")[1]
            try:
                parts = time_val.split(":")
                elapsed = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
            except (ValueError, IndexError):
                continue
        else:
            continue

        if source_duration > 0:
            progress = min((elapsed / source_duration) * 100.0, 99.0)
        else:
            progress = 0.0

        if progress - last_progress >= 1.0:
            last_progress = progress
            try:
                db = db_session_factory()
                if rendition_id:
                    rend = db.query(AssetRendition).filter(
                        AssetRendition.id == rendition_id).first()
                    if rend:
                        rend.transcode_progress = round(progress, 1)
                else:
                    asset = db.query(Asset).filter(Asset.id == asset_id).first()
                    if asset:
                        asset.transcode_progress = round(progress, 1)
                db.commit()
                db.close()
            except Exception as db_error:
                logger.debug("Failed to update progress for asset %d: %s",
                             asset_id, db_error)

    stderr_bytes = await proc.stderr.read()
    await proc.wait()
    return proc.returncode, stderr_bytes.decode()


# ── FFmpeg command builders ──────────────────────────────────────────────────
# All builders accept explicit encoding parameters instead of reading globals,
# enabling multi-rendition output at different resolution/codec/bitrate combos.

def _get_codec_flags(codec: str, bitrate: str, resolution: str,
                     framerate: int) -> list:
    """Build codec-specific encoding flags for video.

    H.264 and H.265 have different rate control syntax. This centralizes
    the difference so command builders don't need to branch on codec.
    """
    target_width, target_height = resolution.split("x")
    # Calculate numeric bitrate for bufsize
    bitrate_num = bitrate.rstrip("MmKk")

    if codec == "h265":
        # libx265 uses -x265-params for VBV/rate control
        bufsize_val = int(float(bitrate_num) * 1000) if "M" in bitrate or "m" in bitrate else int(bitrate_num)
        maxrate_val = bufsize_val
        return [
            "-c:v", "libx265",
            "-profile:v", "main",
            "-preset", "slow",
            "-b:v", bitrate,
            "-x265-params",
            f"vbv-bufsize={bufsize_val * 2}:vbv-maxrate={maxrate_val}:"
            f"nal-hrd=cbr:min-keyint={framerate}:keyint={framerate}",
            "-pix_fmt", "yuv420p",
        ]
    else:
        # Reverted to the original simple libx264 config that worked
        # cleanly on VITEC EP6 hardware decoders. Previous iterations
        # added increasingly strict broadcast flags (nal-hrd=cbr, filler
        # NALs, forced keyint, dump_extra BSF, muxrate padding) trying to
        # fix browser-source macroblocking, but those changes actually
        # introduced macroblocking in the playlist path. The original
        # Main profile / loose VBR / x264 defaults works because ffmpeg's
        # mpegts muxer handles the MP4→TS remux correctly when we don't
        # fight it with conflicting rate-control and BSF overrides.
        bufsize = f"{int(float(bitrate_num) * 2)}M" if "M" in bitrate or "m" in bitrate else f"{int(float(bitrate_num) * 2)}k"
        return [
            "-c:v", "libx264",
            "-profile:v", "main",
            "-preset", "medium",
            "-bf", "0",                     # No B-frames: prevents TS muxer from
                                            # doubling timebase (tbr=120) during
                                            # MP4→MPEG-TS remux, and ensures
                                            # DTS==PTS for clean concat transitions
            "-b:v", bitrate,
            "-maxrate", bitrate,
            "-bufsize", bufsize,            # 2x bitrate (standard loose VBV)
            "-pix_fmt", "yuv420p",
        ]


def _video_transcode_cmd(input_path: str, output_path: str,
                          resolution: str, codec: str, bitrate: str,
                          framerate: int, with_progress: bool = False,
                          has_audio: bool = True) -> list:
    """Build ffmpeg command to transcode a video file to a specific profile."""
    target_width, target_height = resolution.split("x")
    cmd = [config.FFMPEG_PATH, "-y"]
    if with_progress:
        cmd += ["-progress", "pipe:1", "-nostats"]
    cmd += ["-i", input_path]
    if not has_audio:
        cmd += ["-f", "lavfi", "-i",
                f"anullsrc=r={config.TRANSCODE_AUDIO_SAMPLERATE}:cl=stereo"]
    cmd += _get_codec_flags(codec, bitrate, resolution, framerate)
    cmd += [
        "-vf", (f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
                f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:black,"
                f"fps={framerate}"),
        "-c:a", config.TRANSCODE_AUDIO_CODEC,
        "-b:a", config.TRANSCODE_AUDIO_BITRATE,
        "-ac", config.TRANSCODE_AUDIO_CHANNELS,
        "-ar", config.TRANSCODE_AUDIO_SAMPLERATE,
    ]
    if not has_audio:
        cmd += ["-shortest"]
    cmd += ["-movflags", "+faststart", "-f", "mp4", output_path]
    return cmd


def _image_to_video_cmd(input_path: str, output_path: str,
                         resolution: str, codec: str, bitrate: str,
                         framerate: int) -> list:
    """Build ffmpeg command to convert a static image into a video clip."""
    target_width, target_height = resolution.split("x")
    duration_seconds = str(config.STATIC_IMAGE_DURATION)
    cmd = [
        config.FFMPEG_PATH, "-y",
        "-loop", "1", "-i", input_path,
        "-f", "lavfi", "-t", duration_seconds,
        "-i", f"anullsrc=r={config.TRANSCODE_AUDIO_SAMPLERATE}:cl=stereo",
    ]
    cmd += _get_codec_flags(codec, bitrate, resolution, framerate)
    cmd += [
        "-t", duration_seconds,
        "-vf", (f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
                f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:black,"
                f"fps={framerate}"),
        "-c:a", config.TRANSCODE_AUDIO_CODEC,
        "-b:a", config.TRANSCODE_AUDIO_BITRATE,
        "-shortest", "-movflags", "+faststart", "-f", "mp4", output_path,
    ]
    return cmd


def _audio_to_video_cmd(input_path: str, output_path: str,
                         resolution: str, codec: str, bitrate: str,
                         framerate: int, with_progress: bool = False) -> list:
    """Convert audio file to video: black screen + source audio."""
    target_width, target_height = resolution.split("x")
    cmd = [config.FFMPEG_PATH, "-y"]
    if with_progress:
        cmd += ["-progress", "pipe:1", "-nostats"]
    cmd += [
        "-f", "lavfi",
        "-i", f"color=c=black:s={target_width}x{target_height}:r={framerate}",
        "-i", input_path,
    ]
    cmd += _get_codec_flags(codec, bitrate, resolution, framerate)
    cmd += [
        "-c:a", config.TRANSCODE_AUDIO_CODEC,
        "-b:a", config.TRANSCODE_AUDIO_BITRATE,
        "-ac", config.TRANSCODE_AUDIO_CHANNELS,
        "-ar", config.TRANSCODE_AUDIO_SAMPLERATE,
        "-shortest",
        "-movflags", "+faststart", "-f", "mp4", output_path,
    ]
    return cmd


def _thumbnail_cmd(input_path: str, output_path: str, asset_type: AssetType) -> list:
    """Build ffmpeg command to generate a 320x180 thumbnail."""
    base = [config.FFMPEG_PATH, "-y"]
    if asset_type == AssetType.AUDIO:
        return base + [
            "-f", "lavfi", "-i", "color=c=0x111827:s=320x180:d=1",
            "-frames:v", "1", output_path,
        ]
    base += ["-i", input_path]
    if asset_type == AssetType.VIDEO:
        base += ["-ss", "00:00:01"]
    base += [
        "-vf", "scale=320:180:force_original_aspect_ratio=decrease,"
               "pad=320:180:(ow-iw)/2:(oh-ih)/2:black",
        "-frames:v", "1", output_path,
    ]
    return base


# ── Probe utilities ──────────────────────────────────────────────────────────

async def _is_animated_gif(file_path: str) -> bool:
    """Detect whether a GIF file contains multiple frames (animated)."""
    cmd = [
        config.FFPROBE_PATH, "-v", "quiet",
        "-count_frames",
        "-select_streams", "v:0",
        "-show_entries", "stream=nb_read_frames",
        "-print_format", "json",
        file_path,
    ]
    rc, stdout, _ = await _run_command(cmd)
    if rc != 0:
        return False
    try:
        data = json.loads(stdout)
        frames = int(data["streams"][0]["nb_read_frames"])
        return frames > 1
    except (json.JSONDecodeError, KeyError, IndexError, ValueError):
        return False


async def probe_media(file_path: str) -> dict:
    """Run ffprobe to extract media metadata."""
    cmd = [config.FFPROBE_PATH, "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", file_path]
    rc, stdout, stderr = await _run_command(cmd)
    if rc != 0:
        return {}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {}


def _has_audio_stream(probe_data: dict) -> bool:
    """Check whether the probed media file contains an audio stream."""
    for stream in probe_data.get("streams", []):
        if stream.get("codec_type") == "audio":
            return True
    return False


def _extract_metadata(probe_data: dict) -> dict:
    """Extract duration, width, height from ffprobe output."""
    meta = {}
    fmt = probe_data.get("format", {})
    if "duration" in fmt:
        meta["duration_seconds"] = float(fmt["duration"])
    for stream in probe_data.get("streams", []):
        if stream.get("codec_type") == "video":
            meta["width"] = stream.get("width")
            meta["height"] = stream.get("height")
            if "duration_seconds" not in meta and "duration" in stream:
                meta["duration_seconds"] = float(stream["duration"])
            break
    return meta


def _load_ladder_config(db: Session) -> dict:
    """Load the transcode ladder config from server settings."""
    from backend.models import ServerSetting
    setting = db.query(ServerSetting).filter(
        ServerSetting.key == "transcode_ladder"
    ).first()
    if setting:
        try:
            return json.loads(setting.value)
        except json.JSONDecodeError:
            pass
    # Default: 720p H.264 + 1080p H.264
    return {"720p_h264": True, "1080p_h264": True, "1080p_h265": False, "4k_h265": False}


# ── Main transcode pipeline ─────────────────────────────────────────────────

async def transcode_asset(asset_id: int, db_session_factory) -> None:
    """Background task: transcode an uploaded asset into a multi-rendition ladder.

    Acquires the concurrency semaphore, then generates all configured
    renditions. Each rendition is an independent ffmpeg transcode at a
    specific resolution/codec/framerate combination.
    """
    async with _transcode_semaphore:
        await _do_transcode(asset_id, db_session_factory)


async def _do_transcode(asset_id: int, db_session_factory) -> None:
    """Inner transcode logic — called under the concurrency semaphore."""
    db: Session = db_session_factory()
    try:
        asset = db.query(Asset).filter(Asset.id == asset_id).first()
        if asset is None:
            return

        asset.status = AssetStatus.PROCESSING
        asset.transcode_progress = 0.0
        db.commit()

        raw_path = str(config.UPLOAD_DIR / asset.original_filename)

        # GIF handling: animated GIFs become VIDEO, static stay as IMAGE
        if Path(raw_path).suffix.lower() == ".gif":
            if await _is_animated_gif(raw_path):
                logger.info("Asset %d is an animated GIF — treating as video", asset_id)
                asset.asset_type = AssetType.VIDEO
                db.commit()

        is_image = (asset.asset_type == AssetType.IMAGE)
        is_audio = (asset.asset_type == AssetType.AUDIO)

        # Probe source for duration and stream info
        source_duration = 0.0
        has_audio = True
        native_width = 1920
        native_height = 1080
        if not is_image:
            source_probe = await probe_media(raw_path)
            source_meta = _extract_metadata(source_probe)
            source_duration = source_meta.get("duration_seconds", 0.0)
            has_audio = _has_audio_stream(source_probe)
            native_width = source_meta.get("width", 1920) or 1920
            native_height = source_meta.get("height", 1080) or 1080
            asset.source_duration_seconds = source_duration
            asset.width = native_width
            asset.height = native_height
            db.commit()
        else:
            source_duration = float(config.STATIC_IMAGE_DURATION)

        # Generate thumbnail
        thumb_path = str(config.THUMBNAIL_DIR / f"thumb_{asset.id}.jpg")
        rc, _, _ = await _run_command(_thumbnail_cmd(raw_path, thumb_path, asset.asset_type))
        if rc == 0:
            asset.thumbnail_path = thumb_path

        # Determine which renditions to generate from the ladder config
        ladder_config = _load_ladder_config(db)
        renditions = get_transcode_ladder(native_width, native_height, ladder_config)
        logger.info("Asset %d: native %dx%d, generating %d renditions: %s",
                     asset_id, native_width, native_height, len(renditions),
                     [(r, c, f) for r, c, f in renditions])

        # Track total progress across all renditions
        total_renditions = len(renditions)
        completed_renditions = 0
        any_success = False
        first_ready_path = None

        for resolution, codec, framerate in renditions:
            # Create rendition record
            bitrate = get_effective_bitrate(resolution, framerate, codec)
            rendition = AssetRendition(
                asset_id=asset_id,
                resolution=resolution,
                codec=codec,
                framerate=framerate,
                status=RenditionStatus.PROCESSING,
                transcode_progress=0.0,
            )
            db.add(rendition)
            db.commit()
            db.refresh(rendition)

            # Build output path includes framerate to distinguish 30/60fps renditions
            res_label = resolution.replace("x", "_")
            out_filename = f"asset_{asset_id}_{res_label}_{codec}_{framerate}fps.mp4"
            out_path = str(config.MEDIA_DIR / out_filename)

            logger.info("Asset %d: transcoding rendition %s/%s @ %s %sfps",
                         asset_id, resolution, codec, bitrate, framerate)

            # Build and run the ffmpeg command
            if is_image:
                cmd = _image_to_video_cmd(raw_path, out_path, resolution, codec,
                                           bitrate, framerate)
                rc, _, stderr = await _run_command(cmd)
            elif is_audio:
                cmd = _audio_to_video_cmd(raw_path, out_path, resolution, codec,
                                           bitrate, framerate,
                                           with_progress=True)
                rc, stderr = await _run_with_progress(
                    cmd, source_duration, asset_id, db_session_factory,
                    rendition_id=rendition.id)
            else:
                cmd = _video_transcode_cmd(raw_path, out_path, resolution, codec,
                                            bitrate, framerate,
                                            with_progress=True, has_audio=has_audio)
                rc, stderr = await _run_with_progress(
                    cmd, source_duration, asset_id, db_session_factory,
                    rendition_id=rendition.id)

            # Update rendition status
            db.refresh(rendition)
            if rc != 0:
                logger.error("Rendition %s/%s failed for asset %d: %s",
                             resolution, codec, asset_id, stderr[-500:])
                rendition.status = RenditionStatus.ERROR
                rendition.error_message = stderr[-2000:]
            else:
                # Probe output for file size
                rendition.file_path = out_path
                rendition.status = RenditionStatus.READY
                rendition.transcode_progress = 100.0
                try:
                    rendition.file_size_bytes = os.path.getsize(out_path)
                except OSError:
                    pass
                any_success = True
                if first_ready_path is None:
                    first_ready_path = out_path

            completed_renditions += 1
            # Update overall asset progress (fraction of renditions complete)
            asset_progress = (completed_renditions / total_renditions) * 100.0
            asset.transcode_progress = min(round(asset_progress, 1), 99.0)
            db.commit()

        # Final asset status
        if any_success:
            # Probe the first successful rendition for canonical metadata
            if first_ready_path:
                probe = await probe_media(first_ready_path)
                meta = _extract_metadata(probe)
                asset.duration_seconds = meta.get("duration_seconds")
                if meta.get("width"):
                    asset.width = meta["width"]
                if meta.get("height"):
                    asset.height = meta["height"]
                asset.file_path = first_ready_path
                # Sum file sizes across all ready renditions
                total_size = sum(
                    r.file_size_bytes or 0
                    for r in db.query(AssetRendition).filter(
                        AssetRendition.asset_id == asset_id,
                        AssetRendition.status == RenditionStatus.READY,
                    ).all()
                )
                asset.file_size_bytes = total_size

            asset.status = AssetStatus.READY
            asset.transcode_progress = 100.0
            asset.error_message = None
        else:
            asset.status = AssetStatus.ERROR
            asset.error_message = "All renditions failed"
            asset.transcode_progress = 0.0

        db.commit()

        # Clean up the raw upload
        try:
            os.remove(raw_path)
        except OSError:
            pass
        logger.info("Asset %d: transcode complete (%d/%d renditions ready)",
                     asset_id, completed_renditions, total_renditions)

    except Exception as exc:
        logger.exception("Error transcoding asset %d", asset_id)
        try:
            asset.status = AssetStatus.ERROR
            asset.error_message = str(exc)[:2000]
            asset.transcode_progress = 0.0
            db.commit()
        except Exception as db_error:
            logger.error("Failed to record error status for asset %d: %s", asset_id, db_error)
    finally:
        db.close()
