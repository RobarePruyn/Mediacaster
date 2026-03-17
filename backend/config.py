"""
Configuration management for the Multicast Streamer application.
All settings can be overridden via environment variables prefixed with MCS_.
"""

import os
from pathlib import Path

# Filesystem paths
BASE_DIR = Path(os.getenv("MCS_BASE_DIR", "/opt/multicast-streamer"))
MEDIA_DIR = BASE_DIR / "media"
UPLOAD_DIR = BASE_DIR / "uploads"
THUMBNAIL_DIR = BASE_DIR / "thumbnails"
DATABASE_PATH = BASE_DIR / "db" / "streamer.db"
CONCAT_DIR = BASE_DIR / "playlists"

# Transcode profile — all uploads normalized to this for reliable playout
TRANSCODE_VIDEO_CODEC = "libx264"
TRANSCODE_VIDEO_PROFILE = "main"
TRANSCODE_VIDEO_PRESET = "medium"
TRANSCODE_VIDEO_BITRATE = "8M"
TRANSCODE_VIDEO_MAXRATE = "8M"
TRANSCODE_VIDEO_BUFSIZE = "16M"
TRANSCODE_RESOLUTION = os.getenv("MCS_TRANSCODE_RESOLUTION", "1920x1080")
TRANSCODE_FRAMERATE = os.getenv("MCS_TRANSCODE_FRAMERATE", "30")
TRANSCODE_AUDIO_CODEC = "aac"
TRANSCODE_AUDIO_BITRATE = "128k"
TRANSCODE_AUDIO_CHANNELS = "2"
TRANSCODE_AUDIO_SAMPLERATE = "48000"
STATIC_IMAGE_DURATION = int(os.getenv("MCS_IMAGE_DURATION", "10"))

# Streaming defaults
DEFAULT_MULTICAST_ADDRESS = os.getenv("MCS_DEFAULT_MCAST_ADDR", "239.1.1.1")
DEFAULT_MULTICAST_PORT = int(os.getenv("MCS_DEFAULT_MCAST_PORT", "5000"))
MULTICAST_TTL = int(os.getenv("MCS_MULTICAST_TTL", "16"))
MAX_CONCURRENT_STREAMS = int(os.getenv("MCS_MAX_STREAMS", "8"))
MAX_CPU_UTILIZATION_PERCENT = float(os.getenv("MCS_MAX_CPU_PCT", "80.0"))
MAX_BANDWIDTH_UTILIZATION_PERCENT = float(os.getenv("MCS_MAX_BW_PCT", "80.0"))

# Auth
SECRET_KEY = os.getenv("MCS_SECRET_KEY", "CHANGE-ME-IN-PRODUCTION-please")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("MCS_TOKEN_EXPIRE_MIN", "480"))
DEFAULT_ADMIN_USERNAME = os.getenv("MCS_ADMIN_USER", "admin")
DEFAULT_ADMIN_PASSWORD = os.getenv("MCS_ADMIN_PASS", "changeme")
CORS_ORIGINS = os.getenv("MCS_CORS_ORIGINS", "*").split(",")

# External tool paths
FFMPEG_PATH = os.getenv("MCS_FFMPEG_PATH", "/usr/bin/ffmpeg")
FFPROBE_PATH = os.getenv("MCS_FFPROBE_PATH", "/usr/bin/ffprobe")

# Ensure directories exist at import time
for _dir in [MEDIA_DIR, UPLOAD_DIR, THUMBNAIL_DIR, DATABASE_PATH.parent, CONCAT_DIR]:
    _dir.mkdir(parents=True, exist_ok=True)
