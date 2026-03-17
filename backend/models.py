"""SQLAlchemy ORM models — users, assets, streams, browser sources, assignments, settings."""

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


class AssetStatus(str, enum.Enum):
    UPLOADING = "uploading"
    PROCESSING = "processing"
    READY = "ready"
    ERROR = "error"


class AssetType(str, enum.Enum):
    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"


class StreamStatus(str, enum.Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    ERROR = "error"


class PlaybackMode(str, enum.Enum):
    LOOP = "loop"
    ONESHOT = "oneshot"


class StreamSourceType(str, enum.Enum):
    PLAYLIST = "playlist"
    BROWSER = "browser"


def generate_strong_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%&*"
    password = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%&*"),
    ]
    password += [secrets.choice(alphabet) for _ in range(length - 4)]
    shuffled = list(password)
    secrets.SystemRandom().shuffle(shuffled)
    return "".join(shuffled)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(128), unique=True, nullable=False, index=True)
    hashed_password = Column(String(256), nullable=False)
    is_active = Column(Boolean, default=True)
    is_admin = Column(Boolean, default=False)
    must_change_password = Column(Boolean, default=True)
    auth_provider = Column(String(64), default="local")
    external_id = Column(String(256), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    assets = relationship("Asset", back_populates="owner")
    assigned_streams = relationship("UserStreamAssignment", back_populates="user",
                                     cascade="all, delete-orphan")


class Asset(Base):
    __tablename__ = "assets"
    id = Column(Integer, primary_key=True, index=True)
    original_filename = Column(String(512), nullable=False)
    display_name = Column(String(512), nullable=False)
    file_path = Column(String(1024), nullable=False)
    thumbnail_path = Column(String(1024), nullable=True)
    asset_type = Column(SAEnum(AssetType), nullable=False)
    status = Column(SAEnum(AssetStatus), default=AssetStatus.UPLOADING)
    error_message = Column(Text, nullable=True)
    transcode_progress = Column(Float, default=0.0)
    duration_seconds = Column(Float, nullable=True)
    source_duration_seconds = Column(Float, nullable=True)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    file_size_bytes = Column(Integer, nullable=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow,
                        onupdate=datetime.datetime.utcnow)
    owner = relationship("User", back_populates="assets")
    stream_items = relationship("StreamItem", back_populates="asset",
                                cascade="all, delete-orphan")


class Stream(Base):
    __tablename__ = "streams"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(256), nullable=False, default="Default Stream")
    multicast_address = Column(String(64), nullable=False, default="239.1.1.1")
    multicast_port = Column(Integer, nullable=False, default=5000)
    status = Column(SAEnum(StreamStatus), default=StreamStatus.STOPPED)
    playback_mode = Column(SAEnum(PlaybackMode), default=PlaybackMode.LOOP)
    source_type = Column(SAEnum(StreamSourceType), default=StreamSourceType.PLAYLIST)
    ffmpeg_pid = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow,
                        onupdate=datetime.datetime.utcnow)
    items = relationship("StreamItem", back_populates="stream",
                         cascade="all, delete-orphan",
                         order_by="StreamItem.position")
    browser_source = relationship("BrowserSource", back_populates="stream",
                                   uselist=False, cascade="all, delete-orphan")
    assigned_users = relationship("UserStreamAssignment", back_populates="stream",
                                   cascade="all, delete-orphan")


class StreamItem(Base):
    __tablename__ = "stream_items"
    id = Column(Integer, primary_key=True, index=True)
    stream_id = Column(Integer, ForeignKey("streams.id", ondelete="CASCADE"), nullable=False)
    asset_id = Column(Integer, ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)
    position = Column(Integer, nullable=False, default=0)
    stream = relationship("Stream", back_populates="items")
    asset = relationship("Asset", back_populates="stream_items")


class BrowserSource(Base):
    """Configuration for a virtual browser capture source."""
    __tablename__ = "browser_sources"
    id = Column(Integer, primary_key=True, index=True)
    stream_id = Column(Integer, ForeignKey("streams.id", ondelete="CASCADE"),
                       nullable=False, unique=True)
    url = Column(String(2048), nullable=False, default="about:blank")
    capture_audio = Column(Boolean, default=False)
    display_number = Column(Integer, nullable=True)    # Xvfb display :N
    vnc_port = Column(Integer, nullable=True)           # x11vnc port for noVNC
    novnc_port = Column(Integer, nullable=True)         # noVNC websocket proxy port
    stream = relationship("Stream", back_populates="browser_source")


class UserStreamAssignment(Base):
    __tablename__ = "user_stream_assignments"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    stream_id = Column(Integer, ForeignKey("streams.id", ondelete="CASCADE"), nullable=False)
    user = relationship("User", back_populates="assigned_streams")
    stream = relationship("Stream", back_populates="assigned_users")


class ServerSetting(Base):
    __tablename__ = "server_settings"
    key = Column(String(128), primary_key=True)
    value = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
