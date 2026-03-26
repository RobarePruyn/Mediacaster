"""
SQLAlchemy ORM models for the Multicast Streamer application.

Defines the complete data model:
  - **User**: Authentication accounts with RBAC (admin vs regular user)
  - **Folder**: Nested media directories with optional sharing (read-only/read-write)
  - **Asset**: Uploaded media files (video, image, audio) with transcode status
  - **Stream**: A multicast output channel — either playlist-based or browser-based
  - **StreamItem**: An ordered entry in a playlist stream (links Asset to Stream)
  - **BrowserSource**: Configuration for a virtual browser capture container
  - **UserStreamAssignment**: Many-to-many mapping of users to their permitted streams
  - **ServerSetting**: Key-value store for runtime-adjustable server configuration

Relationships:
  User 1──* Asset (ownership)
  User 1──* Folder (ownership)
  Folder 1──* Folder (nesting via parent_id)
  Folder 1──* Asset (organization, SET NULL on folder delete)
  User *──* Stream (via UserStreamAssignment — RBAC access control)
  Stream 1──* StreamItem *──1 Asset (playlist contents)
  Stream 1──0..1 BrowserSource (browser capture config, only for browser-type streams)
"""

import datetime
import enum
import secrets
import string
from sqlalchemy import (
    Column, Integer, String, DateTime, Float, Boolean,
    ForeignKey, Enum as SAEnum, Text
)
from sqlalchemy.orm import relationship
from backend.database import Base


# ── Enumerations ──────────────────────────────────────────────────────────────
# These Python enums map to VARCHAR columns in SQLite. Using str as a mixin
# (e.g., ``str, enum.Enum``) ensures the values serialize as strings in JSON.

class AssetStatus(str, enum.Enum):
    """Lifecycle states for an uploaded asset."""
    UPLOADING = "uploading"    # File received, not yet transcoded
    PROCESSING = "processing"  # ffmpeg transcode in progress
    READY = "ready"            # Transcoded and available for playout
    ERROR = "error"            # Transcode failed — see error_message


class AssetType(str, enum.Enum):
    """Media type classification, determined at upload time from MIME type."""
    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"


class StreamStatus(str, enum.Enum):
    """Lifecycle states for a multicast stream."""
    STOPPED = "stopped"      # Not outputting
    STARTING = "starting"    # ffmpeg/container is launching
    RUNNING = "running"      # Actively sending multicast packets
    ERROR = "error"          # Process crashed or failed to start


class PlaybackMode(str, enum.Enum):
    """How a playlist stream behaves when it reaches the end."""
    LOOP = "loop"        # Restart from the beginning (continuous playout)
    ONESHOT = "oneshot"  # Stop after playing through once


class StreamSourceType(str, enum.Enum):
    """Determines how a stream generates its video content."""
    PLAYLIST = "playlist"        # Concatenated media assets via ffmpeg
    BROWSER = "browser"          # Virtual Firefox instance captured via x11grab
    PRESENTATION = "presentation"  # LibreOffice Impress slideshow captured via x11grab


class VideoCodec(str, enum.Enum):
    """Video codec for stream output encoding."""
    H264 = "h264"    # Broad compatibility, lower CPU for encoding
    H265 = "h265"    # ~40% bitrate savings, required for 4K


class RenditionStatus(str, enum.Enum):
    """Lifecycle states for an asset rendition."""
    PROCESSING = "processing"  # Transcode in progress
    READY = "ready"            # Transcoded and available for playout
    ERROR = "error"            # Transcode failed


class PresentationStatus(str, enum.Enum):
    """Lifecycle states for an uploaded presentation."""
    UPLOADING = "uploading"    # File received, not yet converted
    PROCESSING = "processing"  # LibreOffice conversion in progress
    READY = "ready"            # Slides extracted and available
    ERROR = "error"            # Conversion failed — see error_message


class FolderShareMode(str, enum.Enum):
    """Access level when a folder is shared with non-owner users."""
    READ_ONLY = "read_only"    # Can view/use assets but not add/remove/rename
    READ_WRITE = "read_write"  # Can add/remove/rename assets within the folder


# ── Utility functions ─────────────────────────────────────────────────────────

def generate_strong_password(length: int = 16) -> str:
    """
    Generate a cryptographically secure random password.

    Used when creating new user accounts — the generated password is shown
    once to the admin and the user must change it on first login.

    The password is guaranteed to contain at least one uppercase letter,
    one lowercase letter, one digit, and one special character.

    Args:
        length: Total password length (minimum 4 to satisfy all character
                class requirements).

    Returns:
        A random password string of the specified length.
    """
    alphabet = string.ascii_letters + string.digits + "!@#$%&*"
    # Guarantee at least one character from each required class
    password = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%&*"),
    ]
    # Fill remaining length with random characters from the full alphabet
    password += [secrets.choice(alphabet) for _ in range(length - 4)]
    # Shuffle to avoid predictable positions for the guaranteed characters
    shuffled = list(password)
    secrets.SystemRandom().shuffle(shuffled)
    return "".join(shuffled)


# ── ORM Models ────────────────────────────────────────────────────────────────

class User(Base):
    """
    A user account for authentication and RBAC.

    Admin users can create/configure streams, manage other users, and view
    monitoring data. Regular users can only upload assets and manage playlists
    on streams they've been explicitly assigned to.
    """
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(128), unique=True, nullable=False, index=True)
    hashed_password = Column(String(256), nullable=False)  # bcrypt hash
    is_active = Column(Boolean, default=True)     # Deactivated users cannot log in
    is_admin = Column(Boolean, default=False)     # Admin vs regular user
    must_change_password = Column(Boolean, default=True)  # Forces password change on next login
    # Future: support LDAP/OAuth providers via auth_provider + external_id
    auth_provider = Column(String(64), default="local")
    external_id = Column(String(256), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Relationships
    assets = relationship("Asset", back_populates="owner")
    folders = relationship("Folder", back_populates="owner", cascade="all, delete-orphan")
    assigned_streams = relationship("UserStreamAssignment", back_populates="user",
                                     cascade="all, delete-orphan")


class Folder(Base):
    """
    A media directory for organizing assets.

    Supports nesting via parent_id (self-referential foreign key). Each folder
    is owned by the user who created it. Admins can view all folders. Folders
    can be shared with other users in read-only or read-write mode.

    When a folder is deleted, its assets become unfiled (folder_id = NULL)
    rather than being deleted.
    """
    __tablename__ = "folders"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(256), nullable=False)
    parent_id = Column(Integer, ForeignKey("folders.id", ondelete="CASCADE"), nullable=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    is_shared = Column(Boolean, default=False)
    share_mode = Column(SAEnum(FolderShareMode), default=FolderShareMode.READ_ONLY)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow,
                        onupdate=datetime.datetime.utcnow)

    # Relationships
    parent = relationship("Folder", remote_side="Folder.id", backref="children")
    owner = relationship("User", back_populates="folders")
    assets = relationship("Asset", back_populates="folder")


class Asset(Base):
    """
    An uploaded media file (video, image, or audio).

    All uploads go through a transcode pipeline that normalizes them to a
    common H.264/AAC profile. The ``status`` field tracks transcode progress:
    UPLOADING -> PROCESSING -> READY (or ERROR).

    Assets are owned by the user who uploaded them. Regular users can only
    see their own assets; admins can see all assets.
    """
    __tablename__ = "assets"

    id = Column(Integer, primary_key=True, index=True)
    original_filename = Column(String(512), nullable=False)  # Name as uploaded
    display_name = Column(String(512), nullable=False)       # User-editable display name
    file_path = Column(String(1024), nullable=False)         # Path to transcoded file in media/
    thumbnail_path = Column(String(1024), nullable=True)     # Path to thumbnail in thumbnails/
    asset_type = Column(SAEnum(AssetType), nullable=False)
    status = Column(SAEnum(AssetStatus), default=AssetStatus.UPLOADING)
    error_message = Column(Text, nullable=True)              # Populated on transcode failure
    transcode_progress = Column(Float, default=0.0)          # 0.0 to 1.0, updated during transcode
    duration_seconds = Column(Float, nullable=True)          # Duration of transcoded output
    source_duration_seconds = Column(Float, nullable=True)   # Duration of original upload
    width = Column(Integer, nullable=True)                   # Video width in pixels
    height = Column(Integer, nullable=True)                  # Video height in pixels
    file_size_bytes = Column(Integer, nullable=True)         # Size of transcoded file
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    folder_id = Column(Integer, ForeignKey("folders.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow,
                        onupdate=datetime.datetime.utcnow)

    # Relationships
    owner = relationship("User", back_populates="assets")
    folder = relationship("Folder", back_populates="assets")
    # cascade delete-orphan: if the asset is deleted, remove all playlist entries
    stream_items = relationship("StreamItem", back_populates="asset",
                                cascade="all, delete-orphan")
    # Multiple transcoded renditions at different resolution/codec combinations
    renditions = relationship("AssetRendition", back_populates="asset",
                              cascade="all, delete-orphan")


class AssetRendition(Base):
    """
    A transcoded rendition of an asset at a specific resolution and codec.

    The transcode ladder generates multiple renditions per upload (e.g., a
    1080p source produces 1080p/h264 + 1080p/h265 + 720p/h264 renditions).
    Playlist streams use -c copy from the rendition matching their encoding
    profile, avoiding any runtime re-encoding.
    """
    __tablename__ = "asset_renditions"

    id = Column(Integer, primary_key=True, index=True)
    asset_id = Column(Integer, ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)
    resolution = Column(String(16), nullable=False)       # e.g. "1920x1080"
    codec = Column(String(8), nullable=False)             # "h264" or "h265"
    framerate = Column(Integer, nullable=False, default=60)
    file_path = Column(String(1024), nullable=True)       # Path to transcoded file
    file_size_bytes = Column(Integer, nullable=True)
    status = Column(SAEnum(RenditionStatus), default=RenditionStatus.PROCESSING)
    error_message = Column(Text, nullable=True)
    transcode_progress = Column(Float, default=0.0)       # 0.0 to 1.0
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Relationships
    asset = relationship("Asset", back_populates="renditions")


class Stream(Base):
    """
    A multicast output channel.

    Each stream sends MPEG-TS packets to a unique multicast_address:multicast_port.
    The ``source_type`` determines how video content is generated:
      - PLAYLIST: ffmpeg reads from a concat-demuxer playlist of transcoded assets
      - BROWSER: A Podman container runs Firefox + x11grab to capture a web page

    The ``ffmpeg_pid`` is stored so the process can be monitored and killed.
    """
    __tablename__ = "streams"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(256), nullable=False, default="Default Stream")
    multicast_address = Column(String(64), nullable=False, default="239.1.1.1")
    multicast_port = Column(Integer, nullable=False, default=5000)
    status = Column(SAEnum(StreamStatus), default=StreamStatus.STOPPED)
    playback_mode = Column(SAEnum(PlaybackMode), default=PlaybackMode.LOOP)
    source_type = Column(SAEnum(StreamSourceType), default=StreamSourceType.PLAYLIST)
    ffmpeg_pid = Column(Integer, nullable=True)  # PID of the ffmpeg or container process
    # Per-stream encoding profile — determines output resolution, codec, and quality.
    # Defaults produce a clean 1080p30 H.264 stream suitable for most endpoints.
    resolution = Column(String(16), nullable=False, default="1920x1080")   # 3840x2160, 1920x1080, 1280x720
    codec = Column(String(8), nullable=False, default="h264")              # h264 or h265
    framerate = Column(Integer, nullable=False, default=30)                # 30 or 60
    video_bitrate = Column(String(16), nullable=True)   # Override auto-default (e.g. "8M"), null = use table
    gop_size = Column(Integer, nullable=True)            # Override auto-default, null = framerate (1s GOP)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow,
                        onupdate=datetime.datetime.utcnow)

    # Relationships
    # order_by ensures playlist items come back in position order
    items = relationship("StreamItem", back_populates="stream",
                         cascade="all, delete-orphan",
                         order_by="StreamItem.position")
    # uselist=False because a stream has at most one browser source config
    browser_source = relationship("BrowserSource", back_populates="stream",
                                   uselist=False, cascade="all, delete-orphan")
    # Many-to-many user assignments for RBAC
    assigned_users = relationship("UserStreamAssignment", back_populates="stream",
                                   cascade="all, delete-orphan")


class StreamItem(Base):
    """
    A single entry in a playlist stream's playback order.

    Links an Asset to a Stream at a specific position. The ``position``
    column determines playback order (0-indexed, ascending).
    """
    __tablename__ = "stream_items"

    id = Column(Integer, primary_key=True, index=True)
    stream_id = Column(Integer, ForeignKey("streams.id", ondelete="CASCADE"), nullable=False)
    asset_id = Column(Integer, ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)
    position = Column(Integer, nullable=False, default=0)

    # Relationships
    stream = relationship("Stream", back_populates="items")
    asset = relationship("Asset", back_populates="stream_items")


class Presentation(Base):
    """
    An uploaded slideshow file (PPTX, ODP, PDF) converted to per-slide PNG images.

    LibreOffice headless converts the upload into individual slide images stored
    in a dedicated directory. The current_slide field tracks which slide is being
    displayed — the slide viewer HTML page polls this value and the frontend
    control panel updates it via the navigate API.

    Presentations are linked to browser sources via BrowserSource.presentation_id.
    The browser source container loads the slide viewer URL, which displays the
    current slide as a full-screen image.
    """
    __tablename__ = "presentations"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(512), nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    file_path = Column(String(1024), nullable=True)  # Path to the stored PPTX/ODP/PDF file
    slide_count = Column(Integer, default=0)
    current_slide = Column(Integer, default=1)  # 1-indexed
    slides_dir = Column(String(1024), nullable=True)  # Legacy — was used for slide PNGs
    status = Column(SAEnum(PresentationStatus), default=PresentationStatus.UPLOADING)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow,
                        onupdate=datetime.datetime.utcnow)

    # Relationships
    owner = relationship("User")
    browser_sources = relationship("BrowserSource", back_populates="presentation")


class BrowserSource(Base):
    """
    Configuration for a virtual browser capture source.

    When a stream's source_type is BROWSER, this record stores the URL to
    load in Firefox and the allocated display/port numbers for the Podman
    container's Xvfb, VNC, and noVNC services.

    Optionally links to a Presentation — when presentation_id is set, the URL
    is auto-generated to point at the slide viewer page, and the frontend shows
    a slide navigation control panel instead of the manual URL input.

    One-to-one with Stream (unique constraint on stream_id).
    """
    __tablename__ = "browser_sources"

    id = Column(Integer, primary_key=True, index=True)
    stream_id = Column(Integer, ForeignKey("streams.id", ondelete="CASCADE"),
                       nullable=False, unique=True)
    url = Column(String(2048), nullable=False, default="about:blank")
    capture_audio = Column(Boolean, default=False)        # Enable PulseAudio capture in container
    display_number = Column(Integer, nullable=True)       # Xvfb display :N inside the container
    vnc_port = Column(Integer, nullable=True)             # x11vnc port (5950-6050 range)
    novnc_port = Column(Integer, nullable=True)           # noVNC websocket proxy port (6080-6180 range)
    presentation_id = Column(Integer, ForeignKey("presentations.id", ondelete="SET NULL"),
                             nullable=True)

    # Relationships
    stream = relationship("Stream", back_populates="browser_source")
    presentation = relationship("Presentation", back_populates="browser_sources")


class UserStreamAssignment(Base):
    """
    Many-to-many mapping between users and streams for RBAC.

    A regular (non-admin) user can only view, modify playlists, and start/stop
    streams they have been explicitly assigned to by an admin.
    """
    __tablename__ = "user_stream_assignments"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    stream_id = Column(Integer, ForeignKey("streams.id", ondelete="CASCADE"), nullable=False)

    # Relationships
    user = relationship("User", back_populates="assigned_streams")
    stream = relationship("Stream", back_populates="assigned_users")


class ServerSetting(Base):
    """
    Key-value store for runtime-adjustable server configuration.

    Admins can change these via the Settings API without restarting the
    server. Values are stored as strings and parsed to the appropriate
    type when applied to the runtime config module.

    Examples: transcode_resolution, multicast_ttl, max_concurrent_streams.
    """
    __tablename__ = "server_settings"

    key = Column(String(128), primary_key=True)       # Setting identifier (e.g., "transcode_resolution")
    value = Column(Text, nullable=False)               # String representation of the value
    description = Column(Text, nullable=True)          # Human-readable description for the UI
