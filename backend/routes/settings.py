"""
Settings and monitoring routes.

GET  /api/settings           — Get all server settings
PUT  /api/settings           — Update server settings (admin only)
GET  /api/monitoring         — Get system resource utilization + stream breakdown
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from backend.database import get_db
from backend.models import User, ServerSetting, Stream, StreamStatus
from backend.schemas import (
    ServerSettingResponse, ServerSettingsUpdate, SystemMonitorResponse,
    StreamResourceInfo,
)
from backend.auth import get_current_user
from backend.services.monitor import get_system_stats, get_process_stats, estimate_additional_streams
from backend import config

logger = logging.getLogger("settings")
router = APIRouter(prefix="/api", tags=["settings"])

# ---------------------------------------------------------------------------
# Default settings with descriptions — seeded on first run
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "max_concurrent_streams": {
        "value": str(config.MAX_CONCURRENT_STREAMS),
        "description": "Maximum number of simultaneous multicast streams",
    },
    "transcode_resolution": {
        "value": config.TRANSCODE_RESOLUTION,
        "description": "Target resolution for transcoded assets (WxH)",
    },
    "transcode_framerate": {
        "value": config.TRANSCODE_FRAMERATE,
        "description": "Target framerate for transcoded assets (fps)",
    },
    "transcode_video_bitrate": {
        "value": config.TRANSCODE_VIDEO_BITRATE,
        "description": "Target video bitrate for transcoded assets (e.g. 8M, 4M)",
    },
    "transcode_audio_bitrate": {
        "value": config.TRANSCODE_AUDIO_BITRATE,
        "description": "Target audio bitrate for transcoded assets (e.g. 128k, 256k)",
    },
    "transcode_video_preset": {
        "value": config.TRANSCODE_VIDEO_PRESET,
        "description": "FFmpeg encoding preset (ultrafast, fast, medium, slow, veryslow)",
    },
    "transcode_video_profile": {
        "value": config.TRANSCODE_VIDEO_PROFILE,
        "description": "H.264 profile (baseline, main, high)",
    },
    "static_image_duration": {
        "value": str(config.STATIC_IMAGE_DURATION),
        "description": "Duration in seconds for image-to-video conversion",
    },
    "multicast_ttl": {
        "value": str(config.MULTICAST_TTL),
        "description": "Multicast TTL (time-to-live / hop count)",
    },
    "default_multicast_address": {
        "value": config.DEFAULT_MULTICAST_ADDRESS,
        "description": "Default multicast address for new streams",
    },
    "default_multicast_port": {
        "value": str(config.DEFAULT_MULTICAST_PORT),
        "description": "Default multicast port for new streams",
    },
    "max_cpu_utilization": {
        "value": str(config.MAX_CPU_UTILIZATION_PERCENT),
        "description": "CPU utilization ceiling (%) — used for stream capacity estimates",
    },
    "max_bandwidth_utilization": {
        "value": str(config.MAX_BANDWIDTH_UTILIZATION_PERCENT),
        "description": "Bandwidth utilization ceiling (%) — used for stream capacity estimates",
    },
}


def seed_default_settings(db: Session):
    """Insert default settings that don't already exist in the DB."""
    for key, info in DEFAULT_SETTINGS.items():
        existing = db.query(ServerSetting).filter(ServerSetting.key == key).first()
        if existing is None:
            db.add(ServerSetting(
                key=key, value=info["value"], description=info["description"]
            ))
    db.commit()


def get_setting_value(db: Session, key: str, default: str = "") -> str:
    """Read a single setting value from the DB, falling back to default."""
    setting = db.query(ServerSetting).filter(ServerSetting.key == key).first()
    if setting is None:
        return DEFAULT_SETTINGS.get(key, {}).get("value", default)
    return setting.value


# ---------------------------------------------------------------------------
# Settings endpoints
# ---------------------------------------------------------------------------
@router.get("/settings", response_model=list[ServerSettingResponse])
def list_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get all server settings."""
    settings = db.query(ServerSetting).order_by(ServerSetting.key).all()
    return settings


@router.put("/settings")
def update_settings(
    body: ServerSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update one or more server settings. Admin only."""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")

    updated_keys = []
    for key, value in body.settings.items():
        setting = db.query(ServerSetting).filter(ServerSetting.key == key).first()
        if setting is None:
            raise HTTPException(status_code=404, detail=f"Unknown setting: {key}")
        setting.value = value
        updated_keys.append(key)

    db.commit()

    # Apply runtime-effective settings to the config module
    _apply_runtime_settings(db)

    return {"updated": updated_keys}


def _apply_runtime_settings(db: Session):
    """
    Push DB setting values into the config module so they take effect
    without restarting the service. Only applies to settings that make
    sense to change at runtime.
    """
    try:
        config.MAX_CONCURRENT_STREAMS = int(get_setting_value(db, "max_concurrent_streams", "8"))
        config.TRANSCODE_RESOLUTION = get_setting_value(db, "transcode_resolution", "1920x1080")
        config.TRANSCODE_FRAMERATE = get_setting_value(db, "transcode_framerate", "30")
        config.TRANSCODE_VIDEO_BITRATE = get_setting_value(db, "transcode_video_bitrate", "8M")
        config.TRANSCODE_VIDEO_MAXRATE = config.TRANSCODE_VIDEO_BITRATE
        config.TRANSCODE_AUDIO_BITRATE = get_setting_value(db, "transcode_audio_bitrate", "128k")
        config.TRANSCODE_VIDEO_PRESET = get_setting_value(db, "transcode_video_preset", "medium")
        config.TRANSCODE_VIDEO_PROFILE = get_setting_value(db, "transcode_video_profile", "main")
        config.STATIC_IMAGE_DURATION = int(get_setting_value(db, "static_image_duration", "10"))
        config.MULTICAST_TTL = int(get_setting_value(db, "multicast_ttl", "16"))
        config.DEFAULT_MULTICAST_ADDRESS = get_setting_value(db, "default_multicast_address", "239.1.1.1")
        config.DEFAULT_MULTICAST_PORT = int(get_setting_value(db, "default_multicast_port", "5000"))
        config.MAX_CPU_UTILIZATION_PERCENT = float(get_setting_value(db, "max_cpu_utilization", "80.0"))
        config.MAX_BANDWIDTH_UTILIZATION_PERCENT = float(get_setting_value(db, "max_bandwidth_utilization", "80.0"))
        logger.info("Runtime settings applied from database")
    except Exception as exc:
        logger.error("Failed to apply runtime settings: %s", exc)


# ---------------------------------------------------------------------------
# Monitoring endpoint
# ---------------------------------------------------------------------------
@router.get("/monitoring", response_model=SystemMonitorResponse)
def get_monitoring(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get system resource utilization and per-stream breakdown. Admin only."""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    system_stats = get_system_stats()

    stream_manager = request.app.state.stream_manager
    browser_manager = request.app.state.browser_manager
    from backend.models import StreamSourceType
    streams = db.query(Stream).all()
    active_stream_stats = []
    active_raw_stats = []

    for stream in streams:
        is_browser = (stream.source_type == StreamSourceType.BROWSER)

        if is_browser:
            is_active = browser_manager.is_active(stream.id)
            pids = browser_manager.get_browser_pids(stream.id) if is_active else []
        else:
            is_active = stream_manager.is_stream_active(stream.id)
            runtime = stream_manager.get_status(stream.id)
            pids = [runtime["pid"]] if is_active and runtime.get("pid") else []

        if pids:
            # Aggregate CPU/RAM across all PIDs (browser has multiple processes)
            total_cpu = 0.0
            total_mem = 0.0
            for pid in pids:
                ps = get_process_stats(pid)
                total_cpu += ps["cpu_percent"]
                total_mem += ps["memory_mb"]
            combined = {"cpu_percent": round(total_cpu, 1), "memory_mb": round(total_mem, 1)}
            active_raw_stats.append(combined)
            active_stream_stats.append(StreamResourceInfo(
                stream_id=stream.id,
                stream_name=f"{'🌐 ' if is_browser else ''}{stream.name}",
                pid=pids[0] if pids else None,
                cpu_percent=combined["cpu_percent"],
                memory_mb=combined["memory_mb"],
                status="running",
            ))
        else:
            active_stream_stats.append(StreamResourceInfo(
                stream_id=stream.id,
                stream_name=f"{'🌐 ' if is_browser else ''}{stream.name}",
                status=stream.status.value,
            ))

    # Capacity estimation
    max_cpu = float(get_setting_value(db, "max_cpu_utilization", "80.0"))
    max_mem = float(get_setting_value(db, "max_bandwidth_utilization", "80.0"))
    capacity = estimate_additional_streams(system_stats, active_raw_stats, max_cpu, max_mem)

    return SystemMonitorResponse(
        **system_stats,
        active_streams=active_stream_stats,
        **capacity,
    )
