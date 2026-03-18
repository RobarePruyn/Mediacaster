"""
Pydantic schemas for API request/response validation.

These schemas serve as the contract between the frontend and backend:
  - **Request schemas**: Validate and deserialize incoming JSON bodies
  - **Response schemas**: Serialize ORM models into consistent JSON responses

Organized by domain:
  - Auth / Users: Login, token responses, user CRUD
  - Assets: Upload responses, rename, storage info
  - Streams: Stream CRUD, playlist items, browser source config
  - Server Settings: Runtime configuration key-value pairs
  - Monitoring: System resource metrics and per-stream breakdown

Note: Schemas with ``Config.from_attributes = True`` (formerly ``orm_mode``)
can be constructed directly from SQLAlchemy model instances.
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict
from datetime import datetime


# ── Auth / Users ──────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    """Credentials submitted to the login endpoint."""
    username: str
    password: str


class TokenResponse(BaseModel):
    """
    Returned after successful authentication.

    The ``must_change_password`` flag tells the frontend to redirect
    to the password change form before allowing normal usage.
    """
    access_token: str
    token_type: str = "bearer"
    must_change_password: bool = False


class UserResponse(BaseModel):
    """Public representation of a user account."""
    id: int
    username: str
    is_active: bool
    is_admin: bool
    must_change_password: bool
    auth_provider: str
    created_at: datetime
    # IDs of streams this user is assigned to (for RBAC display in the UI)
    assigned_stream_ids: List[int] = []

    class Config:
        from_attributes = True


class UserCreateRequest(BaseModel):
    """
    Admin request to create a new user.

    The password is auto-generated (not provided by the admin) and
    returned in UserCreateResponse. The new user must change it on first login.
    """
    username: str = Field(min_length=3, max_length=128)
    is_admin: bool = False


class UserCreateResponse(BaseModel):
    """
    Returned after creating a new user.

    Contains the auto-generated password — this is the only time it's
    shown. The admin must communicate it to the user securely.
    """
    user: UserResponse
    generated_password: str


class UserUpdateRequest(BaseModel):
    """Admin request to modify a user's flags (active/admin status)."""
    is_active: Optional[bool] = None
    is_admin: Optional[bool] = None


class UserResetPasswordResponse(BaseModel):
    """Returned after an admin resets a user's password."""
    new_password: str


class ChangePasswordRequest(BaseModel):
    """
    User-initiated password change.

    Requires the current password for verification. The new password
    must be 8-72 characters (72 is bcrypt's max input length).
    """
    current_password: str
    new_password: str = Field(min_length=8, max_length=72)


class UserListResponse(BaseModel):
    """Paginated list of users for the admin user management panel."""
    users: List[UserResponse]
    total_count: int


# ── OIDC / SSO ───────────────────────────────────────────────────────────────

class OIDCCallbackRequest(BaseModel):
    """Request body for the OIDC callback endpoint (code exchange)."""
    code: str
    state: str
    redirect_uri: str


class OIDCConfigResponse(BaseModel):
    """Public OIDC configuration returned to the login page (no auth required)."""
    enabled: bool
    display_name: str


# ── Assets ────────────────────────────────────────────────────────────────────

class AssetResponse(BaseModel):
    """
    Full representation of a media asset.

    Includes transcode status, dimensions, URLs for thumbnail/preview,
    and ownership information. The ``status`` field drives the UI's
    upload progress indicators.
    """
    id: int
    display_name: str
    original_filename: str
    asset_type: str          # "video", "image", or "audio"
    status: str              # "uploading", "processing", "ready", or "error"
    error_message: Optional[str] = None
    transcode_progress: float = 0.0    # 0.0 to 1.0 during processing
    duration_seconds: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    file_size_bytes: Optional[int] = None
    # Thumbnail and preview URLs are constructed by the route handler,
    # not stored in the database (they depend on the server's base URL)
    thumbnail_url: Optional[str] = None
    preview_url: Optional[str] = None
    owner_id: Optional[int] = None
    owner_name: Optional[str] = None
    folder_id: Optional[int] = None
    folder_name: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AssetRename(BaseModel):
    """Request to change an asset's display name."""
    display_name: str = Field(min_length=1, max_length=256)


class AssetListResponse(BaseModel):
    """Paginated list of assets for the media library."""
    assets: List[AssetResponse]
    total_count: int


# ── Storage ───────────────────────────────────────────────────────────────────

class StorageResponse(BaseModel):
    """
    Disk usage information for the media storage directory.

    The ``usable_*`` fields represent 80% of total capacity — we reserve
    20% headroom to prevent the disk from filling completely, which could
    cause ffmpeg writes and database operations to fail.
    """
    total_gb: float
    used_gb: float
    available_gb: float
    usable_gb: float             # 80% of total — our operational ceiling
    usable_remaining_gb: float   # How much space remains before hitting usable_gb
    usage_percent: float         # Current usage as a percentage of usable_gb


# ── Folders ───────────────────────────────────────────────────────────────────

class FolderCreate(BaseModel):
    """Request to create a new media folder."""
    name: str = Field(min_length=1, max_length=256)
    parent_id: Optional[int] = None


class FolderUpdate(BaseModel):
    """Request to rename a folder or change its parent."""
    name: Optional[str] = Field(default=None, min_length=1, max_length=256)
    parent_id: Optional[int] = None


class FolderShareUpdate(BaseModel):
    """Admin request to set folder sharing (toggle + mode)."""
    is_shared: bool
    share_mode: str = Field(default="read_only")  # "read_only" or "read_write"


class FolderResponse(BaseModel):
    """Representation of a media folder."""
    id: int
    name: str
    parent_id: Optional[int] = None
    owner_id: int
    owner_name: Optional[str] = None
    is_shared: bool = False
    share_mode: str = "read_only"
    asset_count: int = 0
    child_count: int = 0
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class FolderTreeResponse(BaseModel):
    """Folder with nested children for tree rendering."""
    id: int
    name: str
    parent_id: Optional[int] = None
    owner_id: int
    is_shared: bool = False
    share_mode: str = "read_only"
    asset_count: int = 0
    children: List["FolderTreeResponse"] = []

    class Config:
        from_attributes = True


class MoveAssetsRequest(BaseModel):
    """Request to move assets into a folder (or unfiled with folder_id=null)."""
    asset_ids: List[int]
    folder_id: Optional[int] = None


# ── Streams ───────────────────────────────────────────────────────────────────

class StreamCreate(BaseModel):
    """Request to create a new multicast stream (admin only)."""
    name: str = Field(default="Default Stream", max_length=256)
    multicast_address: str = Field(default="239.1.1.1")
    # Port range 1024-65535 to avoid privileged ports
    multicast_port: int = Field(default=5000, ge=1024, le=65535)
    playback_mode: str = Field(default="loop")
    source_type: str = Field(default="playlist")  # "playlist" or "browser"


class StreamUpdate(BaseModel):
    """Request to modify stream configuration (admin only)."""
    name: Optional[str] = Field(default=None, max_length=256)
    multicast_address: Optional[str] = None
    multicast_port: Optional[int] = Field(default=None, ge=1024, le=65535)
    playback_mode: Optional[str] = None


class BrowserSourceConfig(BaseModel):
    """Configuration for a browser source's target URL and audio capture."""
    url: str = Field(default="about:blank", max_length=2048)
    capture_audio: bool = False


class BrowserSourceResponse(BaseModel):
    """
    Browser source configuration including allocated port numbers.

    Port numbers are assigned dynamically when the container starts
    and cleared when it stops.
    """
    url: str
    capture_audio: bool
    display_number: Optional[int] = None  # Xvfb :N display number
    vnc_port: Optional[int] = None        # x11vnc port (for raw VNC clients)
    novnc_port: Optional[int] = None      # noVNC websocket port (for iframe preview)

    class Config:
        from_attributes = True


class StreamItemCreate(BaseModel):
    """Request to add an asset to a stream's playlist."""
    asset_id: int
    position: Optional[int] = None  # If omitted, appended to the end


class StreamItemReorder(BaseModel):
    """
    Request to reorder a stream's entire playlist.

    The ``asset_ids`` list defines the new order — position is inferred
    from the list index. All current items must be included.
    """
    asset_ids: List[int]


class StreamItemResponse(BaseModel):
    """A single playlist entry with its associated asset details."""
    id: int
    asset_id: int
    position: int
    asset: AssetResponse  # Nested full asset info for the UI

    class Config:
        from_attributes = True


class StreamResponse(BaseModel):
    """
    Full representation of a multicast stream.

    Includes playlist items (for playlist type), browser source config
    (for browser type), and the list of assigned user IDs for RBAC.
    """
    id: int
    name: str
    multicast_address: str
    multicast_port: int
    status: str                # "stopped", "starting", "running", or "error"
    playback_mode: str         # "loop" or "oneshot"
    source_type: str = "playlist"  # "playlist" or "browser"
    items: List[StreamItemResponse] = []
    browser_source: Optional[BrowserSourceResponse] = None
    assigned_user_ids: List[int] = []  # For RBAC display in the admin UI
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class StreamListResponse(BaseModel):
    """Paginated list of streams."""
    streams: List[StreamResponse]
    total_count: int


class StreamAssignRequest(BaseModel):
    """Admin request to set which users are assigned to a stream."""
    user_ids: List[int]


# ── Server Settings ───────────────────────────────────────────────────────────

class ServerSettingResponse(BaseModel):
    """A single server setting with its current value and description."""
    key: str
    value: str
    description: Optional[str] = None

    class Config:
        from_attributes = True


class ServerSettingsUpdate(BaseModel):
    """
    Batch update for server settings.

    The ``settings`` dict maps setting keys to their new string values.
    Only provided keys are updated; others remain unchanged.
    """
    settings: Dict[str, str]


# ── Monitoring ────────────────────────────────────────────────────────────────

class StreamResourceInfo(BaseModel):
    """Per-stream resource consumption (CPU, memory) from psutil."""
    stream_id: int
    stream_name: str
    pid: Optional[int] = None      # ffmpeg or container PID (None if stopped)
    cpu_percent: float = 0.0       # CPU usage percentage for this stream's process
    memory_mb: float = 0.0         # RSS memory in megabytes
    status: str = "stopped"


class SystemMonitorResponse(BaseModel):
    """
    System-wide resource metrics and capacity planning data.

    Used by the Monitoring dashboard to display live bar meters and
    estimate how many additional streams the server can handle.
    """
    cpu_percent: float                        # Overall system CPU usage
    cpu_count: int                            # Number of logical CPU cores
    memory_total_mb: float                    # Total system RAM
    memory_used_mb: float                     # Currently used RAM
    memory_percent: float                     # RAM usage percentage
    network_tx_mbps: float                    # Network transmit rate (Mbps)
    network_rx_mbps: float                    # Network receive rate (Mbps)
    active_streams: List[StreamResourceInfo]  # Per-stream breakdown
    estimated_additional_streams: int         # How many more streams can fit
    headroom_cpu_percent: float               # Remaining CPU before hitting limit
    headroom_memory_percent: float            # Remaining memory before hitting limit
    headroom_bandwidth_percent: float         # Remaining TX bandwidth before hitting limit
