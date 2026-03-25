"""
Server settings and system monitoring routes.

Provides:
- GET  /api/settings    — Get all server settings (any authenticated user)
- PUT  /api/settings    — Update one or more server settings (admin only)
- GET  /api/monitoring  — Get system resource utilization + per-stream breakdown (admin only)

Settings are stored in the ServerSetting table as key-value pairs with descriptions.
On first run, seed_default_settings() populates any missing settings from DEFAULT_SETTINGS.
When an admin updates settings via PUT, _apply_runtime_settings() pushes the new values
into the in-memory config module so they take effect immediately without a service restart.

The monitoring endpoint aggregates system-wide CPU/RAM/network stats with per-stream
resource usage by looking up PIDs from the stream and browser managers, then uses
psutil to get per-process stats.
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
# Each key maps to a dict with "value" (string) and "description" (human-readable).
# These defaults come from the config module's initial values, which can be
# overridden via MCS_* environment variables at startup.
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
    "browser_source_video_bitrate": {
        "value": config.BROWSER_SOURCE_VIDEO_BITRATE,
        "description": "Video bitrate for browser source live encoding (e.g. 15M, 20M)",
    },
    "browser_source_video_preset": {
        "value": config.BROWSER_SOURCE_VIDEO_PRESET,
        "description": "FFmpeg preset for browser source encoding (veryfast, faster, fast, medium)",
    },
    "browser_source_video_tune": {
        "value": config.BROWSER_SOURCE_VIDEO_TUNE,
        "description": "FFmpeg tune for browser source encoding (blank for none, zerolatency, film, stillimage)",
    },
    "browser_source_audio_bitrate": {
        "value": config.BROWSER_SOURCE_AUDIO_BITRATE,
        "description": "Audio bitrate for browser source encoding (e.g. 128k, 192k)",
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
    "max_memory_utilization": {
        "value": str(config.MAX_MEMORY_UTILIZATION_PERCENT),
        "description": "Memory utilization ceiling (%) — used for stream capacity estimates",
    },
    "max_bandwidth_utilization": {
        "value": str(config.MAX_BANDWIDTH_UTILIZATION_PERCENT),
        "description": "Bandwidth utilization ceiling (%) — used for stream capacity estimates",
    },
    "network_link_speed_mbps": {
        "value": str(config.NETWORK_LINK_SPEED_MBPS),
        "description": "NIC link speed in Mbps (e.g. 1000 for 1 Gbps) — used for bandwidth % calculation",
    },
    # OIDC / SSO settings — generic OpenID Connect Authorization Code flow
    "oidc_enabled": {
        "value": str(config.OIDC_ENABLED).lower(),
        "description": "Enable OIDC single sign-on (true/false)",
    },
    "oidc_discovery_url": {
        "value": config.OIDC_DISCOVERY_URL,
        "description": "OIDC discovery URL (e.g. https://idp.example.com/.well-known/openid-configuration)",
    },
    "oidc_client_id": {
        "value": config.OIDC_CLIENT_ID,
        "description": "OIDC client ID registered with the identity provider",
    },
    "oidc_client_secret": {
        "value": config.OIDC_CLIENT_SECRET,
        "description": "OIDC client secret (keep confidential)",
    },
    "oidc_display_name": {
        "value": config.OIDC_DISPLAY_NAME,
        "description": "Label shown on the SSO login button (e.g. 'Corporate SSO', 'Okta')",
    },
}


def seed_default_settings(db: Session):
    """Insert default settings that don't already exist in the DB.

    Called during app startup (lifespan). Only inserts missing keys,
    so existing values (previously changed by an admin) are preserved.
    """
    for key, info in DEFAULT_SETTINGS.items():
        existing = db.query(ServerSetting).filter(ServerSetting.key == key).first()
        if existing is None:
            db.add(ServerSetting(
                key=key, value=info["value"], description=info["description"]
            ))
    db.commit()


def get_setting_value(db: Session, key: str, default: str = "") -> str:
    """Read a single setting value from the DB, falling back to the default.

    Args:
        db: Active database session.
        key: The setting key to look up (e.g. "max_concurrent_streams").
        default: Fallback value if the key exists neither in the DB nor in DEFAULT_SETTINGS.

    Returns:
        The setting value as a string (callers are responsible for type conversion).
    """
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
    """Get all server settings. Available to any authenticated user.

    Regular users see settings for informational purposes (e.g. transcode
    resolution), but only admins can modify them via the PUT endpoint.
    """
    settings = db.query(ServerSetting).order_by(ServerSetting.key).all()
    return settings


@router.put("/settings")
def update_settings(
    body: ServerSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update one or more server settings. Admin only.

    Accepts a dict of {key: value} pairs. All keys must already exist
    in the DB (seeded at startup) — unknown keys return 404 to prevent
    typos from silently creating orphan settings.

    After committing to the DB, _apply_runtime_settings() pushes the
    new values into the config module so they take effect immediately
    for subsequent transcode jobs and stream operations.
    """
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

    # Push updated values into the in-memory config module
    _apply_runtime_settings(db)

    return {"updated": updated_keys}


def _apply_runtime_settings(db: Session):
    """Push DB setting values into the config module for immediate effect.

    This avoids requiring a service restart when an admin changes settings.
    Each setting is read from the DB (with a fallback default) and written
    to the corresponding config module attribute. Type conversions (int, float)
    are applied as needed.

    If any setting fails to parse (e.g. non-numeric value for an int field),
    the error is logged but other settings are still applied.
    """
    try:
        config.MAX_CONCURRENT_STREAMS = int(get_setting_value(db, "max_concurrent_streams", "8"))
        config.TRANSCODE_RESOLUTION = get_setting_value(db, "transcode_resolution", "1920x1080")
        config.TRANSCODE_FRAMERATE = get_setting_value(db, "transcode_framerate", "30")
        config.TRANSCODE_VIDEO_BITRATE = get_setting_value(db, "transcode_video_bitrate", "8M")
        # maxrate mirrors bitrate to enforce CBR-like behavior in ffmpeg
        config.TRANSCODE_VIDEO_MAXRATE = config.TRANSCODE_VIDEO_BITRATE
        config.TRANSCODE_AUDIO_BITRATE = get_setting_value(db, "transcode_audio_bitrate", "128k")
        config.TRANSCODE_VIDEO_PRESET = get_setting_value(db, "transcode_video_preset", "medium")
        config.TRANSCODE_VIDEO_PROFILE = get_setting_value(db, "transcode_video_profile", "main")
        config.STATIC_IMAGE_DURATION = int(get_setting_value(db, "static_image_duration", "10"))
        # Browser source live encoding settings
        config.BROWSER_SOURCE_VIDEO_BITRATE = get_setting_value(db, "browser_source_video_bitrate", "15M")
        config.BROWSER_SOURCE_VIDEO_PRESET = get_setting_value(db, "browser_source_video_preset", "faster")
        config.BROWSER_SOURCE_VIDEO_TUNE = get_setting_value(db, "browser_source_video_tune", "")
        config.BROWSER_SOURCE_AUDIO_BITRATE = get_setting_value(db, "browser_source_audio_bitrate", "128k")
        config.MULTICAST_TTL = int(get_setting_value(db, "multicast_ttl", "16"))
        config.DEFAULT_MULTICAST_ADDRESS = get_setting_value(db, "default_multicast_address", "239.1.1.1")
        config.DEFAULT_MULTICAST_PORT = int(get_setting_value(db, "default_multicast_port", "5000"))
        config.MAX_CPU_UTILIZATION_PERCENT = float(get_setting_value(db, "max_cpu_utilization", "80.0"))
        config.MAX_MEMORY_UTILIZATION_PERCENT = float(get_setting_value(db, "max_memory_utilization", "80.0"))
        config.MAX_BANDWIDTH_UTILIZATION_PERCENT = float(get_setting_value(db, "max_bandwidth_utilization", "80.0"))
        config.NETWORK_LINK_SPEED_MBPS = float(get_setting_value(db, "network_link_speed_mbps", "1000.0"))
        # OIDC / SSO settings
        config.OIDC_ENABLED = get_setting_value(db, "oidc_enabled", "false").lower() == "true"
        config.OIDC_DISCOVERY_URL = get_setting_value(db, "oidc_discovery_url", "")
        config.OIDC_CLIENT_ID = get_setting_value(db, "oidc_client_id", "")
        config.OIDC_CLIENT_SECRET = get_setting_value(db, "oidc_client_secret", "")
        config.OIDC_DISPLAY_NAME = get_setting_value(db, "oidc_display_name", "SSO")
        logger.info("Runtime settings applied from database")
    except (ValueError, TypeError) as exc:
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
    """Get system resource utilization and per-stream breakdown. Admin only.

    Aggregates:
    1. System-wide stats (CPU, RAM, network) via psutil
    2. Per-stream resource usage by looking up PIDs from the stream/browser managers
    3. Capacity estimate: how many more streams the server can handle based on
       current usage and the configured CPU/memory utilization ceilings

    Browser source streams may have multiple PIDs (Xvfb, Firefox, ffmpeg, etc.),
    so their CPU/memory is aggregated across all container processes.
    """
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    system_stats = get_system_stats()

    stream_manager = request.app.state.stream_manager
    browser_manager = request.app.state.browser_manager
    from backend.models import StreamSourceType
    streams = db.query(Stream).all()
    active_stream_stats = []
    active_raw_stats = []  # Raw stats for capacity estimation (active streams only)

    for stream in streams:
        is_browser = (stream.source_type == StreamSourceType.BROWSER)

        # Determine if the stream is active and get its PIDs from the appropriate manager
        if is_browser:
            is_active = browser_manager.is_active(stream.id)
            # Browser containers have multiple PIDs (Xvfb, Firefox, ffmpeg, etc.)
            pids = browser_manager.get_browser_pids(stream.id) if is_active else []
        else:
            is_active = stream_manager.is_stream_active(stream.id)
            runtime = stream_manager.get_status(stream.id)
            # Playlist streams have a single ffmpeg PID
            pids = [runtime["pid"]] if is_active and runtime.get("pid") else []

        if pids:
            # Aggregate CPU/RAM across all PIDs for this stream
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
            # Include stopped/idle streams in the list with no resource data
            active_stream_stats.append(StreamResourceInfo(
                stream_id=stream.id,
                stream_name=f"{'🌐 ' if is_browser else ''}{stream.name}",
                status=stream.status.value,
            ))

    # Estimate how many more streams can fit within the configured utilization ceilings
    max_cpu = float(get_setting_value(db, "max_cpu_utilization", "80.0"))
    max_mem = float(get_setting_value(db, "max_memory_utilization", "80.0"))
    max_bw = float(get_setting_value(db, "max_bandwidth_utilization", "80.0"))
    link_speed = float(get_setting_value(db, "network_link_speed_mbps", "1000.0"))
    capacity = estimate_additional_streams(
        system_stats, active_raw_stats, max_cpu, max_mem, max_bw, link_speed
    )

    return SystemMonitorResponse(
        **system_stats,
        active_streams=active_stream_stats,
        **capacity,
    )
