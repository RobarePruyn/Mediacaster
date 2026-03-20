"""
Transcoding service — normalizes all uploaded media to a uniform H.264/AAC profile at ingest time.

This ensures every asset in the system has identical codec, resolution, framerate, and audio
parameters, which is critical for seamless ffmpeg concat-demuxer playout (the stream manager
can use -c copy instead of re-encoding on the fly).

Supports three asset types:
  - Video: re-encodes to H.264/AAC at target resolution/bitrate
  - Image: converts to a looping video clip (black + image for configured duration)
  - Audio: generates black video frames + source audio, same profile as video

All transcode parameters come from backend.config (overridable via MCS_* env vars).
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

# Limit concurrent transcodes to avoid saturating CPU/memory when multiple
# users upload simultaneously. Queued transcodes wait here until a slot opens.
# 2 concurrent is a reasonable default — each ffmpeg instance uses 2-4 CPU cores
# with the "medium" preset, so 2 concurrent transcodes already saturate an 8-core server.
_transcode_semaphore = asyncio.Semaphore(2)

# Recognized file extensions by media type — used to classify uploads before transcoding
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".ts", ".m2ts", ".mxf",
                    ".flv", ".wmv", ".webm", ".mpg", ".mpeg", ".m4v"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".aac", ".flac", ".ogg", ".opus", ".m4a", ".wma", ".aiff"}
# GIF is handled specially — animated GIFs are transcoded as video, static as image.
# It's listed separately so classify_upload can defer to probe-time detection.
GIF_EXTENSION = {".gif"}
ALL_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS | GIF_EXTENSION


def classify_upload(filename: str) -> AssetType:
    """
    Determine the asset type from a filename's extension.

    GIF files are initially classified as IMAGE. The transcode pipeline
    re-classifies animated GIFs as VIDEO after probing (see _do_transcode).

    Args:
        filename: Original upload filename (e.g. "clip.mp4")

    Returns:
        AssetType enum value (VIDEO, IMAGE, or AUDIO)

    Raises:
        ValueError: If the file extension is not in any recognized set
    """
    ext = Path(filename).suffix.lower()
    if ext in VIDEO_EXTENSIONS:
        return AssetType.VIDEO
    elif ext in IMAGE_EXTENSIONS or ext in GIF_EXTENSION:
        return AssetType.IMAGE
    elif ext in AUDIO_EXTENSIONS:
        return AssetType.AUDIO
    raise ValueError(f"Unsupported file type: {ext}")


async def _run_command(command: list) -> tuple:
    """
    Run a subprocess asynchronously and capture all output.

    Args:
        command: Command and arguments as a list (e.g. ["ffmpeg", "-y", ...])

    Returns:
        Tuple of (return_code, stdout_string, stderr_string)
    """
    logger.info("Running: %s", " ".join(command))
    proc = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout_bytes, stderr_bytes = await proc.communicate()
    return proc.returncode, stdout_bytes.decode(), stderr_bytes.decode()


async def _run_with_progress(command: list, source_duration: float,
                              asset_id: int, db_session_factory) -> tuple:
    """
    Run an ffmpeg command while parsing real-time progress from stdout.

    ffmpeg's `-progress pipe:1` flag outputs key=value lines to stdout, including
    `out_time_us` (microseconds encoded so far) and `out_time` (HH:MM:SS.mmm).
    We parse these to calculate percentage complete and write it to the DB so the
    frontend can display a live progress bar.

    Progress updates are batched — we only write to DB when progress changes by
    at least 1% to avoid excessive DB writes during long transcodes.

    Args:
        command: Full ffmpeg command with -progress pipe:1 included
        source_duration: Duration of the source media in seconds (for percentage calc)
        asset_id: Database ID of the Asset being transcoded
        db_session_factory: Callable that returns a new SQLAlchemy Session

    Returns:
        Tuple of (return_code, stderr_string)
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

        # ffmpeg outputs progress in two possible formats — try microseconds first
        # (more precise), fall back to the HH:MM:SS timestamp format
        if decoded.startswith("out_time_us="):
            try:
                # out_time_us is microseconds of encoded output so far
                elapsed = int(decoded.split("=")[1]) / 1_000_000
            except (ValueError, IndexError):
                continue
        elif decoded.startswith("out_time="):
            time_val = decoded.split("=")[1]
            try:
                # Parse "HH:MM:SS.microseconds" format to total seconds
                parts = time_val.split(":")
                elapsed = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
            except (ValueError, IndexError):
                continue
        else:
            continue

        if source_duration > 0:
            # Cap at 99% — the final 100% is set after transcode completes successfully
            progress = min((elapsed / source_duration) * 100.0, 99.0)
        else:
            progress = 0.0

        # Only update DB when progress changes by at least 1% to reduce write overhead
        if progress - last_progress >= 1.0:
            last_progress = progress
            try:
                db = db_session_factory()
                asset = db.query(Asset).filter(Asset.id == asset_id).first()
                if asset:
                    asset.transcode_progress = round(progress, 1)
                    db.commit()
                db.close()
            except Exception as db_error:
                # Non-fatal: progress display is cosmetic, don't let DB issues kill the transcode
                logger.debug("Failed to update transcode progress for asset %d: %s",
                             asset_id, db_error)

    stderr_bytes = await proc.stderr.read()
    await proc.wait()
    return proc.returncode, stderr_bytes.decode()


def _video_transcode_cmd(input_path: str, output_path: str,
                          with_progress: bool = False,
                          has_audio: bool = True) -> list:
    """
    Build the ffmpeg command to transcode a video file to the standard profile.

    The video filter chain does three things in order:
      1. scale: Fit to target resolution while preserving aspect ratio
      2. pad:  Letterbox/pillarbox with black bars to fill the exact target resolution
      3. fps:  Force constant framerate (required for concat-demuxer playout)

    If the source has no audio stream, silent audio is generated via anullsrc
    so the output always has both video and audio PIDs (required for MPEG-TS
    receivers and seamless concat-demuxer playout).

    Args:
        input_path: Path to the raw uploaded video file
        output_path: Destination path for the transcoded .mp4
        with_progress: If True, add -progress pipe:1 for real-time progress parsing
        has_audio: Whether the source file contains an audio stream

    Returns:
        Complete ffmpeg command as a list of strings
    """
    target_width, target_height = config.TRANSCODE_RESOLUTION.split("x")
    cmd = [config.FFMPEG_PATH, "-y"]
    if with_progress:
        # -progress pipe:1 writes progress key=value pairs to stdout
        # -nostats suppresses the default stderr progress line to avoid clutter
        cmd += ["-progress", "pipe:1", "-nostats"]
    cmd += ["-i", input_path]
    # If the source has no audio, generate silent audio so the output always
    # has both PIDs. MPEG-TS receivers and concat-demuxer playout require it.
    if not has_audio:
        cmd += ["-f", "lavfi", "-i",
                f"anullsrc=r={config.TRANSCODE_AUDIO_SAMPLERATE}:cl=stereo"]
    cmd += [
        "-c:v", config.TRANSCODE_VIDEO_CODEC,
        "-profile:v", config.TRANSCODE_VIDEO_PROFILE,
        "-preset", config.TRANSCODE_VIDEO_PRESET,
        "-b:v", config.TRANSCODE_VIDEO_BITRATE,
        "-maxrate", config.TRANSCODE_VIDEO_MAXRATE,
        # bufsize controls the VBV buffer — set to 2x bitrate for smooth CBR-like output
        "-bufsize", config.TRANSCODE_VIDEO_BUFSIZE,
        "-vf", (f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
                f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:black,"
                f"fps={config.TRANSCODE_FRAMERATE}"),
        # GOP size = framerate → 1-second keyframe interval for fast IPTV channel tune-in.
        # Receivers must decode from a keyframe, so shorter GOPs reduce channel-change latency
        # at the cost of slightly higher bitrate (more I-frames).
        "-g", config.TRANSCODE_FRAMERATE,
        "-c:a", config.TRANSCODE_AUDIO_CODEC,
        "-b:a", config.TRANSCODE_AUDIO_BITRATE,
        "-ac", config.TRANSCODE_AUDIO_CHANNELS,
        "-ar", config.TRANSCODE_AUDIO_SAMPLERATE,
    ]
    # -shortest needed when using anullsrc (infinite source) so output ends
    # when the video finishes
    if not has_audio:
        cmd += ["-shortest"]
    cmd += ["-movflags", "+faststart", "-f", "mp4", output_path]
    return cmd


def _animated_gif_to_video_cmd(input_path: str, output_path: str,
                                with_progress: bool = False) -> list:
    """
    Build ffmpeg command to transcode an animated GIF to the standard video profile.

    Animated GIFs have no audio stream, so we generate silent audio with anullsrc
    (same approach as _audio_to_video_cmd). The -ignore_loop 0 flag tells ffmpeg
    to play the GIF once rather than looping infinitely.

    Args:
        input_path: Path to the animated GIF file
        output_path: Destination path for the transcoded .mp4
        with_progress: If True, add -progress pipe:1 for real-time progress parsing

    Returns:
        Complete ffmpeg command as a list of strings
    """
    target_width, target_height = config.TRANSCODE_RESOLUTION.split("x")
    cmd = [config.FFMPEG_PATH, "-y"]
    if with_progress:
        cmd += ["-progress", "pipe:1", "-nostats"]
    cmd += [
        # -ignore_loop 0 plays the GIF animation once (not infinitely)
        "-ignore_loop", "0",
        "-i", input_path,
        # Generate silent audio to match the GIF duration — MPEG-TS receivers
        # expect both audio and video PIDs
        "-f", "lavfi", "-i", f"anullsrc=r={config.TRANSCODE_AUDIO_SAMPLERATE}:cl=stereo",
        "-c:v", config.TRANSCODE_VIDEO_CODEC,
        "-profile:v", config.TRANSCODE_VIDEO_PROFILE,
        "-preset", config.TRANSCODE_VIDEO_PRESET,
        "-b:v", config.TRANSCODE_VIDEO_BITRATE,
        "-maxrate", config.TRANSCODE_VIDEO_MAXRATE,
        "-bufsize", config.TRANSCODE_VIDEO_BUFSIZE,
        "-vf", (f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
                f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:black,"
                f"fps={config.TRANSCODE_FRAMERATE}"),
        "-pix_fmt", "yuv420p",
        "-g", config.TRANSCODE_FRAMERATE,
        "-c:a", config.TRANSCODE_AUDIO_CODEC,
        "-b:a", config.TRANSCODE_AUDIO_BITRATE,
        "-ac", config.TRANSCODE_AUDIO_CHANNELS,
        "-ar", config.TRANSCODE_AUDIO_SAMPLERATE,
        # -shortest: end when the GIF animation finishes (anullsrc is infinite)
        "-shortest",
        "-movflags", "+faststart", "-f", "mp4", output_path,
    ]
    return cmd


def _image_to_video_cmd(input_path: str, output_path: str) -> list:
    """
    Build ffmpeg command to convert a static image into a video clip.

    The image is looped for STATIC_IMAGE_DURATION seconds with silent audio
    (anullsrc generates silence). This produces a playable video clip that
    can be seamlessly concatenated with other assets in a playlist.

    Args:
        input_path: Path to the uploaded image file
        output_path: Destination path for the generated .mp4

    Returns:
        Complete ffmpeg command as a list of strings
    """
    target_width, target_height = config.TRANSCODE_RESOLUTION.split("x")
    duration_seconds = str(config.STATIC_IMAGE_DURATION)
    return [
        config.FFMPEG_PATH, "-y",
        # -loop 1 makes ffmpeg repeat the single image frame continuously
        "-loop", "1", "-i", input_path,
        # Generate silent stereo audio to match the image duration
        "-f", "lavfi", "-t", duration_seconds,
        "-i", f"anullsrc=r={config.TRANSCODE_AUDIO_SAMPLERATE}:cl=stereo",
        "-c:v", config.TRANSCODE_VIDEO_CODEC,
        "-profile:v", config.TRANSCODE_VIDEO_PROFILE,
        "-preset", config.TRANSCODE_VIDEO_PRESET,
        "-b:v", config.TRANSCODE_VIDEO_BITRATE,
        "-maxrate", config.TRANSCODE_VIDEO_MAXRATE,
        "-bufsize", config.TRANSCODE_VIDEO_BUFSIZE,
        "-t", duration_seconds,
        "-vf", (f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
                f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:black,"
                f"fps={config.TRANSCODE_FRAMERATE}"),
        # yuv420p is required for broad compatibility (some encoders default to yuv444p for stills)
        "-pix_fmt", "yuv420p",
        # 1-second GOP for fast IPTV tune-in (same as video transcode)
        "-g", config.TRANSCODE_FRAMERATE,
        "-c:a", config.TRANSCODE_AUDIO_CODEC,
        "-b:a", config.TRANSCODE_AUDIO_BITRATE,
        # -shortest: end when the shorter stream (audio, capped by -t) finishes
        "-shortest", "-movflags", "+faststart", "-f", "mp4", output_path,
    ]


def _audio_to_video_cmd(input_path: str, output_path: str,
                         with_progress: bool = False) -> list:
    """
    Convert an audio file to a video: black screen + source audio, standard profile.

    This allows audio-only uploads to be treated identically to video assets in
    playlists — the stream manager can concat them without special handling.

    Args:
        input_path: Path to the uploaded audio file
        output_path: Destination path for the generated .mp4
        with_progress: If True, add -progress pipe:1 for real-time progress parsing

    Returns:
        Complete ffmpeg command as a list of strings
    """
    target_width, target_height = config.TRANSCODE_RESOLUTION.split("x")
    cmd = [config.FFMPEG_PATH, "-y"]
    if with_progress:
        cmd += ["-progress", "pipe:1", "-nostats"]
    cmd += [
        # Generate black video at target resolution/framerate using lavfi color source
        "-f", "lavfi",
        "-i", f"color=c=black:s={target_width}x{target_height}:r={config.TRANSCODE_FRAMERATE}",
        # Audio source — the actual uploaded file
        "-i", input_path,
        # Video: encode the black frames at the standard profile
        "-c:v", config.TRANSCODE_VIDEO_CODEC,
        "-profile:v", config.TRANSCODE_VIDEO_PROFILE,
        "-preset", config.TRANSCODE_VIDEO_PRESET,
        "-b:v", config.TRANSCODE_VIDEO_BITRATE,
        "-maxrate", config.TRANSCODE_VIDEO_MAXRATE,
        "-bufsize", config.TRANSCODE_VIDEO_BUFSIZE,
        "-pix_fmt", "yuv420p",
        # 1-second GOP for fast IPTV tune-in (same as video transcode)
        "-g", config.TRANSCODE_FRAMERATE,
        # Audio: transcode to AAC at the standard profile
        "-c:a", config.TRANSCODE_AUDIO_CODEC,
        "-b:a", config.TRANSCODE_AUDIO_BITRATE,
        "-ac", config.TRANSCODE_AUDIO_CHANNELS,
        "-ar", config.TRANSCODE_AUDIO_SAMPLERATE,
        # -shortest: end when the audio stream finishes (the color source is infinite)
        "-shortest",
        "-movflags", "+faststart", "-f", "mp4", output_path,
    ]
    return cmd


def _thumbnail_cmd(input_path: str, output_path: str, asset_type: AssetType) -> list:
    """
    Build ffmpeg command to generate a 320x180 thumbnail for the asset library UI.

    Audio assets get a solid dark thumbnail (no visual content to capture).
    Video assets grab a frame at 1 second in (to skip black leader frames).
    Image assets use the image directly.

    Args:
        input_path: Path to the source media file
        output_path: Destination path for the thumbnail JPEG
        asset_type: The classified type of the asset

    Returns:
        Complete ffmpeg command as a list of strings
    """
    base = [config.FFMPEG_PATH, "-y"]
    if asset_type == AssetType.AUDIO:
        # No visual content to thumbnail — generate a dark solid-color placeholder
        # Color 0x111827 matches the dark UI theme background
        target_width, target_height = config.TRANSCODE_RESOLUTION.split("x")
        return base + [
            "-f", "lavfi", "-i", f"color=c=0x111827:s=320x180:d=1",
            "-frames:v", "1", output_path,
        ]
    base += ["-i", input_path]
    if asset_type == AssetType.VIDEO:
        # Seek 1 second in to avoid black leader frames common in many video files
        base += ["-ss", "00:00:01"]
    base += [
        # Same scale+pad logic as transcoding but at thumbnail resolution
        "-vf", "scale=320:180:force_original_aspect_ratio=decrease,"
               "pad=320:180:(ow-iw)/2:(oh-ih)/2:black",
        "-frames:v", "1", output_path,
    ]
    return base


async def _is_animated_gif(file_path: str) -> bool:
    """Detect whether a GIF file contains multiple frames (animated).

    Uses ffprobe to count the number of video frames. A static GIF has
    exactly 1 frame; anything more is animated.

    Args:
        file_path: Path to the GIF file.

    Returns:
        True if the GIF is animated (more than 1 frame), False otherwise.
    """
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
    """
    Run ffprobe to extract media metadata (duration, resolution, codec info).

    Args:
        file_path: Path to the media file to probe

    Returns:
        Parsed JSON dict from ffprobe, or empty dict on failure.
        Contains "format" (duration, bitrate) and "streams" (codec, resolution) keys.
    """
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
    """Check whether the probed media file contains an audio stream.

    Args:
        probe_data: Raw dict from ffprobe JSON output.

    Returns:
        True if at least one audio stream is present.
    """
    for stream in probe_data.get("streams", []):
        if stream.get("codec_type") == "audio":
            return True
    return False


def _extract_metadata(probe_data: dict) -> dict:
    """
    Extract human-relevant metadata fields from raw ffprobe output.

    Pulls duration from the format container level first, then falls back to
    the first video stream's duration if the container doesn't report one
    (some formats like raw H.264 don't have container-level duration).

    Args:
        probe_data: Raw dict from ffprobe JSON output

    Returns:
        Dict with optional keys: duration_seconds, width, height
    """
    meta = {}
    fmt = probe_data.get("format", {})
    if "duration" in fmt:
        meta["duration_seconds"] = float(fmt["duration"])
    # Find the first video stream for resolution info
    for stream in probe_data.get("streams", []):
        if stream.get("codec_type") == "video":
            meta["width"] = stream.get("width")
            meta["height"] = stream.get("height")
            # Fall back to stream-level duration if container didn't have one
            if "duration_seconds" not in meta and "duration" in stream:
                meta["duration_seconds"] = float(stream["duration"])
            break
    return meta


async def transcode_asset(asset_id: int, db_session_factory) -> None:
    """
    Background task: transcode an uploaded asset to the standard H.264/AAC profile.

    This is the main entry point called by the upload route as a background task.
    It handles the full lifecycle: status tracking, thumbnail generation, transcoding,
    metadata extraction, and cleanup of the raw upload file.

    Concurrency is limited by _transcode_semaphore (default 2) to prevent CPU/memory
    saturation when multiple uploads arrive simultaneously. Queued transcodes wait
    for a slot to open before starting.

    The flow is:
      1. Acquire transcode semaphore slot (may wait if at capacity)
      2. Mark asset as PROCESSING
      3. Probe source for duration (needed for progress calculation)
      4. Generate a thumbnail for the asset library
      5. Transcode to standard profile (with live progress for video/audio)
      6. Probe the transcoded output for final metadata
      7. Update asset record with file path, dimensions, duration, size
      8. Delete the raw upload file (the transcoded version is the canonical copy)

    Args:
        asset_id: Database ID of the Asset to transcode
        db_session_factory: Callable that returns a new SQLAlchemy Session
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

        # GIF handling: probe to determine if animated or static.
        # Animated GIFs are reclassified as VIDEO so they go through a dedicated
        # transcode pipeline that generates silent audio (GIFs have no audio stream).
        # Static GIFs stay as IMAGE and get converted to a timed clip like other stills.
        is_animated_gif = False
        if Path(raw_path).suffix.lower() == ".gif":
            animated = await _is_animated_gif(raw_path)
            if animated:
                logger.info("Asset %d is an animated GIF — treating as video", asset_id)
                asset.asset_type = AssetType.VIDEO
                is_animated_gif = True
                db.commit()
            else:
                logger.info("Asset %d is a static GIF — treating as image", asset_id)

        is_image = (asset.asset_type == AssetType.IMAGE)
        is_audio = (asset.asset_type == AssetType.AUDIO)

        # Probe source to get duration and stream info — needed for progress
        # percentage calculation and detecting whether audio is present.
        # Images don't need probing; their duration is the configured STATIC_IMAGE_DURATION.
        source_duration = 0.0
        has_audio = True  # Assume true; overridden by probe for non-image assets
        if not is_image:
            source_probe = await probe_media(raw_path)
            source_meta = _extract_metadata(source_probe)
            source_duration = source_meta.get("duration_seconds", 0.0)
            has_audio = _has_audio_stream(source_probe)
            asset.source_duration_seconds = source_duration
            db.commit()
        else:
            source_duration = float(config.STATIC_IMAGE_DURATION)

        # Generate a thumbnail for the asset library grid view
        thumb_path = str(config.THUMBNAIL_DIR / f"thumb_{asset.id}.jpg")
        rc, _, _ = await _run_command(_thumbnail_cmd(raw_path, thumb_path, asset.asset_type))
        if rc == 0:
            asset.thumbnail_path = thumb_path

        # Transcode to the standard profile
        out_path = str(config.MEDIA_DIR / f"asset_{asset.id}.mp4")

        if is_image:
            # Images are short clips — no progress tracking needed (completes in seconds)
            cmd = _image_to_video_cmd(raw_path, out_path)
            rc, _, stderr = await _run_command(cmd)
        elif is_audio:
            # Audio transcodes can be long — track progress via ffmpeg stdout
            cmd = _audio_to_video_cmd(raw_path, out_path, with_progress=True)
            rc, stderr = await _run_with_progress(cmd, source_duration, asset_id, db_session_factory)
        elif is_animated_gif:
            # Animated GIFs have no audio — use dedicated command with anullsrc
            cmd = _animated_gif_to_video_cmd(raw_path, out_path, with_progress=True)
            rc, stderr = await _run_with_progress(cmd, source_duration, asset_id, db_session_factory)
        else:
            # Video transcodes can be very long — track progress via ffmpeg stdout.
            # has_audio tells the command builder whether to generate silent audio.
            cmd = _video_transcode_cmd(raw_path, out_path, with_progress=True, has_audio=has_audio)
            rc, stderr = await _run_with_progress(cmd, source_duration, asset_id, db_session_factory)

        if rc != 0:
            logger.error("Transcode failed for asset %d: %s", asset_id, stderr)
            asset.status = AssetStatus.ERROR
            # Keep the last 2000 chars of stderr — the actual error message is at
            # the end, after ffmpeg's lengthy build config banner
            asset.error_message = stderr[-2000:]
            asset.transcode_progress = 0.0
            db.commit()
            return

        # Probe the transcoded output to get final metadata (duration, resolution)
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

        # Clean up the raw upload — the transcoded file in media/ is now canonical
        try:
            os.remove(raw_path)
        except OSError:
            # Non-critical: raw file may have already been cleaned up or moved
            pass
        logger.info("Asset %d transcoded OK -> %s", asset_id, out_path)

    except Exception as exc:
        logger.exception("Error transcoding asset %d", asset_id)
        try:
            asset.status = AssetStatus.ERROR
            asset.error_message = str(exc)[:2000]
            asset.transcode_progress = 0.0
            db.commit()
        except Exception as db_error:
            # If we can't even update the error status, log it and move on.
            # The asset will remain in PROCESSING state until manually fixed.
            logger.error("Failed to record error status for asset %d: %s", asset_id, db_error)
    finally:
        db.close()
