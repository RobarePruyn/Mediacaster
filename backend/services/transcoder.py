"""
Transcoding service — normalizes uploads to H.264/AAC at ingest.
Supports video, image, and audio files.
Audio files get black video + the source audio, same profile as everything else.
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from sqlalchemy.orm import Session
from backend import config
from backend.models import Asset, AssetStatus, AssetType

logger = logging.getLogger("transcoder")

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".ts", ".m2ts", ".mxf",
                    ".flv", ".wmv", ".webm", ".mpg", ".mpeg", ".m4v"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".aac", ".flac", ".ogg", ".opus", ".m4a", ".wma", ".aiff"}
ALL_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS


def classify_upload(filename: str) -> AssetType:
    ext = Path(filename).suffix.lower()
    if ext in VIDEO_EXTENSIONS:
        return AssetType.VIDEO
    elif ext in IMAGE_EXTENSIONS:
        return AssetType.IMAGE
    elif ext in AUDIO_EXTENSIONS:
        return AssetType.AUDIO
    raise ValueError(f"Unsupported file type: {ext}")


async def _run_command(command: list) -> tuple:
    logger.info("Running: %s", " ".join(command))
    proc = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout_bytes, stderr_bytes = await proc.communicate()
    return proc.returncode, stdout_bytes.decode(), stderr_bytes.decode()


async def _run_with_progress(command: list, source_duration: float,
                              asset_id: int, db_session_factory) -> tuple:
    """Run ffmpeg with -progress pipe:1 and update transcode_progress in DB."""
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
                asset = db.query(Asset).filter(Asset.id == asset_id).first()
                if asset:
                    asset.transcode_progress = round(progress, 1)
                    db.commit()
                db.close()
            except Exception:
                pass

    stderr_bytes = await proc.stderr.read()
    await proc.wait()
    return proc.returncode, stderr_bytes.decode()


def _video_transcode_cmd(input_path: str, output_path: str,
                          with_progress: bool = False) -> list:
    w, h = config.TRANSCODE_RESOLUTION.split("x")
    cmd = [config.FFMPEG_PATH, "-y"]
    if with_progress:
        cmd += ["-progress", "pipe:1", "-nostats"]
    cmd += [
        "-i", input_path,
        "-c:v", config.TRANSCODE_VIDEO_CODEC,
        "-profile:v", config.TRANSCODE_VIDEO_PROFILE,
        "-preset", config.TRANSCODE_VIDEO_PRESET,
        "-b:v", config.TRANSCODE_VIDEO_BITRATE,
        "-maxrate", config.TRANSCODE_VIDEO_MAXRATE,
        "-bufsize", config.TRANSCODE_VIDEO_BUFSIZE,
        "-vf", (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,"
                f"fps={config.TRANSCODE_FRAMERATE}"),
        "-c:a", config.TRANSCODE_AUDIO_CODEC,
        "-b:a", config.TRANSCODE_AUDIO_BITRATE,
        "-ac", config.TRANSCODE_AUDIO_CHANNELS,
        "-ar", config.TRANSCODE_AUDIO_SAMPLERATE,
        "-movflags", "+faststart", "-f", "mp4", output_path,
    ]
    return cmd


def _image_to_video_cmd(input_path: str, output_path: str) -> list:
    w, h = config.TRANSCODE_RESOLUTION.split("x")
    dur = str(config.STATIC_IMAGE_DURATION)
    return [
        config.FFMPEG_PATH, "-y",
        "-loop", "1", "-i", input_path,
        "-f", "lavfi", "-t", dur,
        "-i", f"anullsrc=r={config.TRANSCODE_AUDIO_SAMPLERATE}:cl=stereo",
        "-c:v", config.TRANSCODE_VIDEO_CODEC,
        "-profile:v", config.TRANSCODE_VIDEO_PROFILE,
        "-preset", config.TRANSCODE_VIDEO_PRESET,
        "-b:v", config.TRANSCODE_VIDEO_BITRATE,
        "-maxrate", config.TRANSCODE_VIDEO_MAXRATE,
        "-bufsize", config.TRANSCODE_VIDEO_BUFSIZE,
        "-t", dur,
        "-vf", (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,"
                f"fps={config.TRANSCODE_FRAMERATE}"),
        "-pix_fmt", "yuv420p",
        "-c:a", config.TRANSCODE_AUDIO_CODEC,
        "-b:a", config.TRANSCODE_AUDIO_BITRATE,
        "-shortest", "-movflags", "+faststart", "-f", "mp4", output_path,
    ]


def _audio_to_video_cmd(input_path: str, output_path: str,
                         with_progress: bool = False) -> list:
    """Convert audio file to video: black screen + source audio, standard profile."""
    w, h = config.TRANSCODE_RESOLUTION.split("x")
    cmd = [config.FFMPEG_PATH, "-y"]
    if with_progress:
        cmd += ["-progress", "pipe:1", "-nostats"]
    cmd += [
        # Generate black video at target resolution/framerate
        "-f", "lavfi",
        "-i", f"color=c=black:s={w}x{h}:r={config.TRANSCODE_FRAMERATE}",
        # Audio source
        "-i", input_path,
        # Video: encode the black frames
        "-c:v", config.TRANSCODE_VIDEO_CODEC,
        "-profile:v", config.TRANSCODE_VIDEO_PROFILE,
        "-preset", config.TRANSCODE_VIDEO_PRESET,
        "-b:v", config.TRANSCODE_VIDEO_BITRATE,
        "-maxrate", config.TRANSCODE_VIDEO_MAXRATE,
        "-bufsize", config.TRANSCODE_VIDEO_BUFSIZE,
        "-pix_fmt", "yuv420p",
        # Audio: transcode to AAC
        "-c:a", config.TRANSCODE_AUDIO_CODEC,
        "-b:a", config.TRANSCODE_AUDIO_BITRATE,
        "-ac", config.TRANSCODE_AUDIO_CHANNELS,
        "-ar", config.TRANSCODE_AUDIO_SAMPLERATE,
        # End when audio ends
        "-shortest",
        "-movflags", "+faststart", "-f", "mp4", output_path,
    ]
    return cmd


def _thumbnail_cmd(input_path: str, output_path: str, asset_type: AssetType) -> list:
    base = [config.FFMPEG_PATH, "-y"]
    if asset_type == AssetType.AUDIO:
        # Generate a dark thumbnail with "AUDIO" feel
        w, h = config.TRANSCODE_RESOLUTION.split("x")
        return base + [
            "-f", "lavfi", "-i", f"color=c=0x111827:s=320x180:d=1",
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


async def probe_media(file_path: str) -> dict:
    cmd = [config.FFPROBE_PATH, "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", file_path]
    rc, stdout, stderr = await _run_command(cmd)
    if rc != 0:
        return {}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {}


def _extract_metadata(probe_data: dict) -> dict:
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


async def transcode_asset(asset_id: int, db_session_factory) -> None:
    """Background task: transcode uploaded asset to standard profile."""
    db: Session = db_session_factory()
    try:
        asset = db.query(Asset).filter(Asset.id == asset_id).first()
        if asset is None:
            return

        asset.status = AssetStatus.PROCESSING
        asset.transcode_progress = 0.0
        db.commit()

        raw_path = str(config.UPLOAD_DIR / asset.original_filename)
        is_image = (asset.asset_type == AssetType.IMAGE)
        is_audio = (asset.asset_type == AssetType.AUDIO)

        # Probe source for duration
        source_duration = 0.0
        if not is_image:
            source_probe = await probe_media(raw_path)
            source_meta = _extract_metadata(source_probe)
            source_duration = source_meta.get("duration_seconds", 0.0)
            asset.source_duration_seconds = source_duration
            db.commit()
        else:
            source_duration = float(config.STATIC_IMAGE_DURATION)

        # Thumbnail
        thumb_path = str(config.THUMBNAIL_DIR / f"thumb_{asset.id}.jpg")
        rc, _, _ = await _run_command(_thumbnail_cmd(raw_path, thumb_path, asset.asset_type))
        if rc == 0:
            asset.thumbnail_path = thumb_path

        # Transcode
        out_path = str(config.MEDIA_DIR / f"asset_{asset.id}.mp4")

        if is_image:
            cmd = _image_to_video_cmd(raw_path, out_path)
            rc, _, stderr = await _run_command(cmd)
        elif is_audio:
            cmd = _audio_to_video_cmd(raw_path, out_path, with_progress=True)
            rc, stderr = await _run_with_progress(cmd, source_duration, asset_id, db_session_factory)
        else:
            cmd = _video_transcode_cmd(raw_path, out_path, with_progress=True)
            rc, stderr = await _run_with_progress(cmd, source_duration, asset_id, db_session_factory)

        if rc != 0:
            logger.error("Transcode failed for asset %d: %s", asset_id, stderr)
            asset.status = AssetStatus.ERROR
            asset.error_message = stderr[:2000]
            asset.transcode_progress = 0.0
            db.commit()
            return

        # Probe transcoded output
        probe = await probe_media(out_path)
        meta = _extract_metadata(probe)

        asset.file_path = out_path
        asset.duration_seconds = meta.get("duration_seconds")
        asset.width = meta.get("width")
        asset.height = meta.get("height")
        asset.file_size_bytes = os.path.getsize(out_path)
        asset.status = AssetStatus.READY
        asset.transcode_progress = 100.0
        asset.error_message = None
        db.commit()

        try:
            os.remove(raw_path)
        except OSError:
            pass
        logger.info("Asset %d transcoded OK -> %s", asset_id, out_path)

    except Exception as exc:
        logger.exception("Error transcoding asset %d", asset_id)
        try:
            asset.status = AssetStatus.ERROR
            asset.error_message = str(exc)[:2000]
            asset.transcode_progress = 0.0
            db.commit()
        except Exception:
            pass
    finally:
        db.close()
