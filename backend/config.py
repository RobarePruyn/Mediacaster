"""
Configuration management for the Multicast Streamer application.

Centralizes all tunable parameters in one place. Every setting can be
overridden via an environment variable prefixed with ``MCS_`` — this allows
deploy.sh and systemd to customize behavior without editing code.

Settings are organized into sections:
  - Filesystem paths (media storage, database, playlists)
  - Transcode profile (ffmpeg encoding parameters for upload normalization)
  - Streaming defaults (multicast addressing, resource limits)
  - Auth (JWT secret, token lifetime, default admin credentials)
  - External tool paths (ffmpeg, ffprobe binaries)

Note: Some of these values (transcode resolution, multicast TTL, etc.) can
also be changed at runtime via the Settings API, which persists them to the
``server_settings`` table. The env vars here set the *initial* defaults.
"""

import os
import secrets
import logging
from pathlib import Path

_config_logger = logging.getLogger("config")

# ── Filesystem paths ──────────────────────────────────────────────────────────
# BASE_DIR is the root installation directory. All data subdirectories live here.
BASE_DIR = Path(os.getenv("MCS_BASE_DIR", "/opt/multicast-streamer"))
MEDIA_DIR = BASE_DIR / "media"            # Transcoded media files ready for playout
UPLOAD_DIR = BASE_DIR / "uploads"          # Raw uploaded files (pre-transcode)
THUMBNAIL_DIR = BASE_DIR / "thumbnails"    # Auto-generated video/image thumbnails
CONCAT_DIR = BASE_DIR / "playlists"        # ffmpeg concat-demuxer playlist text files

# PostgreSQL connection URL. Override with MCS_DATABASE_URL for custom host/port/credentials.
DATABASE_URL = os.getenv(
    "MCS_DATABASE_URL",
    "postgresql://mcs:mcs@localhost:5432/mediacaster"
)

# ── Transcode profile ────────────────────────────────────────────────────────
# All uploaded media is normalized to this profile before playout. This ensures
# every asset has identical codec, resolution, and framerate — required for
# seamless ffmpeg concat-demuxer switching between playlist items.
TRANSCODE_VIDEO_CODEC = "libx264"
TRANSCODE_VIDEO_PROFILE = "main"       # H.264 profile (main = broad compatibility)
TRANSCODE_VIDEO_PRESET = "slow"        # Favor compression quality over encode speed (offline)
TRANSCODE_VIDEO_BITRATE = "8M"         # Target bitrate for CBR-like output
TRANSCODE_VIDEO_MAXRATE = "8M"         # VBV max rate — keeps bitrate predictable
TRANSCODE_VIDEO_BUFSIZE = "16M"        # VBV buffer size — 2x maxrate is typical
TRANSCODE_RESOLUTION = os.getenv("MCS_TRANSCODE_RESOLUTION", "1920x1080")
TRANSCODE_FRAMERATE = os.getenv("MCS_TRANSCODE_FRAMERATE", "30")
TRANSCODE_AUDIO_CODEC = "aac"
TRANSCODE_AUDIO_BITRATE = "128k"
TRANSCODE_AUDIO_CHANNELS = "2"         # Stereo — broadcast standard
TRANSCODE_AUDIO_SAMPLERATE = "48000"   # 48kHz — broadcast standard (not 44.1kHz)

# Duration in seconds for static images when converted to video assets.
# Images become a black video of this length with the image composited on top.
STATIC_IMAGE_DURATION = int(os.getenv("MCS_IMAGE_DURATION", "10"))

# ── Presentation conversion ──────────────────────────────────────────────
PRESENTATIONS_DIR = BASE_DIR / "presentations"  # Per-presentation subdirectories of slide PNGs
LIBREOFFICE_PATH = os.getenv("MCS_LIBREOFFICE_PATH", "/usr/bin/libreoffice")

# ── Browser source live encoding profile ─────────────────────────────────
# Separate from the offline transcode profile because live x11grab encoding
# has different tradeoffs: we can't use slow presets (CPU-bound in real time)
# but we can afford higher bitrate since it's one stream at a time.
# Priority order: quality > avoid macroblocking > minimize latency.
BROWSER_SOURCE_VIDEO_BITRATE = os.getenv("MCS_BROWSER_VIDEO_BITRATE", "20M")
BROWSER_SOURCE_VIDEO_PRESET = os.getenv("MCS_BROWSER_VIDEO_PRESET", "ultrafast")
# Empty string = no tune flag. "zerolatency" saves ~200ms but disables B-frames
# and lookahead, significantly hurting quality. "film" or "stillimage" can help
# for specific content types.
BROWSER_SOURCE_VIDEO_TUNE = os.getenv("MCS_BROWSER_VIDEO_TUNE", "")
BROWSER_SOURCE_AUDIO_BITRATE = os.getenv("MCS_BROWSER_AUDIO_BITRATE", "128k")

# ── Streaming defaults ────────────────────────────────────────────────────────
DEFAULT_MULTICAST_ADDRESS = os.getenv("MCS_DEFAULT_MCAST_ADDR", "239.1.1.1")
DEFAULT_MULTICAST_PORT = int(os.getenv("MCS_DEFAULT_MCAST_PORT", "5000"))
# TTL controls how many router hops multicast packets can traverse.
# 16 is generous for a LAN; production environments may want 1-4.
MULTICAST_TTL = int(os.getenv("MCS_MULTICAST_TTL", "16"))

# Resource guardrails — the system refuses to start new streams if these
# thresholds would be exceeded, preventing server overload.
MAX_CONCURRENT_STREAMS = int(os.getenv("MCS_MAX_STREAMS", "8"))
MAX_CPU_UTILIZATION_PERCENT = float(os.getenv("MCS_MAX_CPU_PCT", "80.0"))
MAX_MEMORY_UTILIZATION_PERCENT = float(os.getenv("MCS_MAX_MEM_PCT", "80.0"))
MAX_BANDWIDTH_UTILIZATION_PERCENT = float(os.getenv("MCS_MAX_BW_PCT", "80.0"))
# Link speed of the primary NIC in Mbps — used for bandwidth capacity estimation.
# Auto-detection is unreliable with bonded/virtual interfaces, so this is manual.
NETWORK_LINK_SPEED_MBPS = float(os.getenv("MCS_LINK_SPEED_MBPS", "1000.0"))

# ── Auth ──────────────────────────────────────────────────────────────────────
# JWT signing key. If the default placeholder is still in use (no MCS_SECRET_KEY env var),
# generate a random key at startup. This means tokens won't survive a server restart
# (all users will need to log in again), which is safer than using a well-known default.
_raw_secret = os.getenv("MCS_SECRET_KEY", "")
if not _raw_secret or _raw_secret == "CHANGE-ME-IN-PRODUCTION-please":
    SECRET_KEY = secrets.token_urlsafe(64)
    _config_logger.warning(
        "MCS_SECRET_KEY not set — generated a random JWT secret. "
        "Tokens will not persist across restarts. Set MCS_SECRET_KEY in the environment "
        "or systemd unit for stable sessions."
    )
else:
    SECRET_KEY = _raw_secret
# Token lifetime: 480 minutes = 8 hours (one broadcast shift)
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("MCS_TOKEN_EXPIRE_MIN", "480"))
# Default admin credentials — created on first run if no admin exists.
# The admin is forced to change the password on first login.
DEFAULT_ADMIN_USERNAME = os.getenv("MCS_ADMIN_USER", "admin")
DEFAULT_ADMIN_PASSWORD = os.getenv("MCS_ADMIN_PASS", "changeme")
# CORS origins — comma-separated list. "*" allows all (fine for dev, restrict in prod).
CORS_ORIGINS = os.getenv("MCS_CORS_ORIGINS", "*").split(",")

# ── OIDC / SSO ────────────────────────────────────────────────────────────────
# Generic OIDC Authorization Code flow for enterprise SSO (Cognito, Azure AD,
# Okta, Keycloak, etc.). These defaults are overwritten from DB settings at
# startup via _apply_runtime_settings() in routes/settings.py.
OIDC_ENABLED = os.getenv("MCS_OIDC_ENABLED", "false").lower() == "true"
OIDC_DISCOVERY_URL = os.getenv("MCS_OIDC_DISCOVERY_URL", "")
OIDC_CLIENT_ID = os.getenv("MCS_OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.getenv("MCS_OIDC_CLIENT_SECRET", "")
OIDC_DISPLAY_NAME = os.getenv("MCS_OIDC_DISPLAY_NAME", "SSO")

# ── External tool paths ───────────────────────────────────────────────────────
# Absolute paths to ffmpeg/ffprobe binaries. Override if they're installed
# in a non-standard location (e.g., a custom build in /usr/local/bin/).
FFMPEG_PATH = os.getenv("MCS_FFMPEG_PATH", "/usr/bin/ffmpeg")
FFPROBE_PATH = os.getenv("MCS_FFPROBE_PATH", "/usr/bin/ffprobe")

# ── Wayland capture pipeline paths ───────────────────────────────────────────
# Native Wayland tools used by wayland_manager.py for browser/presentation
# source capture. wf-recorder and ydotool are source-built to /usr/local/bin
# since they're not available as RPMs on AlmaLinux 10.
WESTON_PATH = os.getenv("MCS_WESTON_PATH", "/usr/bin/weston")
CAGE_PATH = os.getenv("MCS_CAGE_PATH", "/usr/local/bin/cage")
WF_RECORDER_PATH = os.getenv("MCS_WF_RECORDER_PATH", "/usr/local/bin/wf-recorder")
WAYVNC_PATH = os.getenv("MCS_WAYVNC_PATH", "/usr/bin/wayvnc")
YDOTOOL_PATH = os.getenv("MCS_YDOTOOL_PATH", "/usr/local/bin/ydotool")
YDOTOOLD_PATH = os.getenv("MCS_YDOTOOLD_PATH", "/usr/local/bin/ydotoold")
FIREFOX_PATH = os.getenv("MCS_FIREFOX_PATH", "/usr/bin/firefox")
WEBSOCKIFY_PATH = os.getenv("MCS_WEBSOCKIFY_PATH", "/usr/bin/websockify")
# noVNC static files served by websockify for the browser-based VNC client
NOVNC_DIR = os.getenv("MCS_NOVNC_DIR", "/opt/multicast-streamer/novnc")

# ── Directory initialization ─────────────────────────────────────────────────
# Create all required data directories at import time so the rest of the
# application can assume they exist. parents=True handles nested paths,
# exist_ok=True makes it idempotent.
for _dir in [MEDIA_DIR, UPLOAD_DIR, THUMBNAIL_DIR, CONCAT_DIR, PRESENTATIONS_DIR]:
    _dir.mkdir(parents=True, exist_ok=True)
