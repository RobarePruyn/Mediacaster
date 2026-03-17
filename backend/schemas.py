"""Pydantic schemas for API request/response validation."""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict
from datetime import datetime


# --- Auth / Users ---
class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    must_change_password: bool = False

class UserResponse(BaseModel):
    id: int
    username: str
    is_active: bool
    is_admin: bool
    must_change_password: bool
    auth_provider: str
    created_at: datetime
    assigned_stream_ids: List[int] = []
    class Config:
        from_attributes = True

class UserCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=128)
    is_admin: bool = False

class UserCreateResponse(BaseModel):
    """Returns the generated password — only shown once at creation time."""
    user: UserResponse
    generated_password: str

class UserUpdateRequest(BaseModel):
    is_active: Optional[bool] = None
    is_admin: Optional[bool] = None

class UserResetPasswordResponse(BaseModel):
    new_password: str

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=72)

class UserListResponse(BaseModel):
    users: List[UserResponse]
    total_count: int

# --- Assets ---
class AssetResponse(BaseModel):
    id: int
    display_name: str
    original_filename: str
    asset_type: str
    status: str
    error_message: Optional[str] = None
    transcode_progress: float = 0.0
    duration_seconds: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    file_size_bytes: Optional[int] = None
    thumbnail_url: Optional[str] = None
    preview_url: Optional[str] = None
    owner_id: Optional[int] = None
    owner_name: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    class Config:
        from_attributes = True

class AssetRename(BaseModel):
    display_name: str = Field(min_length=1, max_length=256)

class AssetListResponse(BaseModel):
    assets: List[AssetResponse]
    total_count: int

# --- Storage ---
class StorageResponse(BaseModel):
    total_gb: float
    used_gb: float
    available_gb: float
    usable_gb: float        # 80% of total
    usable_remaining_gb: float
    usage_percent: float

# --- Streams ---
class StreamCreate(BaseModel):
    name: str = Field(default="Default Stream", max_length=256)
    multicast_address: str = Field(default="239.1.1.1")
    multicast_port: int = Field(default=5000, ge=1024, le=65535)
    playback_mode: str = Field(default="loop")
    source_type: str = Field(default="playlist")  # "playlist" or "browser"

class StreamUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=256)
    multicast_address: Optional[str] = None
    multicast_port: Optional[int] = Field(default=None, ge=1024, le=65535)
    playback_mode: Optional[str] = None

class BrowserSourceConfig(BaseModel):
    url: str = Field(default="about:blank", max_length=2048)
    capture_audio: bool = False

class BrowserSourceResponse(BaseModel):
    url: str
    capture_audio: bool
    display_number: Optional[int] = None
    vnc_port: Optional[int] = None
    novnc_port: Optional[int] = None
    class Config:
        from_attributes = True

class StreamItemCreate(BaseModel):
    asset_id: int
    position: Optional[int] = None

class StreamItemReorder(BaseModel):
    asset_ids: List[int]

class StreamItemResponse(BaseModel):
    id: int
    asset_id: int
    position: int
    asset: AssetResponse
    class Config:
        from_attributes = True

class StreamResponse(BaseModel):
    id: int
    name: str
    multicast_address: str
    multicast_port: int
    status: str
    playback_mode: str
    source_type: str = "playlist"
    items: List[StreamItemResponse] = []
    browser_source: Optional[BrowserSourceResponse] = None
    assigned_user_ids: List[int] = []
    created_at: datetime
    updated_at: datetime
    class Config:
        from_attributes = True

class StreamListResponse(BaseModel):
    streams: List[StreamResponse]
    total_count: int

class StreamAssignRequest(BaseModel):
    user_ids: List[int]

# --- Server Settings ---
class ServerSettingResponse(BaseModel):
    key: str
    value: str
    description: Optional[str] = None
    class Config:
        from_attributes = True

class ServerSettingsUpdate(BaseModel):
    settings: Dict[str, str]

# --- Monitoring ---
class StreamResourceInfo(BaseModel):
    stream_id: int
    stream_name: str
    pid: Optional[int] = None
    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    status: str = "stopped"

class SystemMonitorResponse(BaseModel):
    cpu_percent: float
    cpu_count: int
    memory_total_mb: float
    memory_used_mb: float
    memory_percent: float
    network_tx_mbps: float
    network_rx_mbps: float
    active_streams: List[StreamResourceInfo]
    estimated_additional_streams: int
    headroom_cpu_percent: float
    headroom_memory_percent: float
