"""
Stream management routes with RBAC and browser source support.

Provides:
- POST /api/streams                       — Create a stream (admin only)
- GET  /api/streams                       — List streams (admin: all, user: assigned only)
- GET  /api/streams/{id}                  — Get stream details
- PUT  /api/streams/{id}                  — Update stream config (admin only)
- DELETE /api/streams/{id}                — Delete stream, stopping it first (admin only)
- PUT  /api/streams/{id}/assign           — Assign users to a stream (admin only)
- PUT  /api/streams/{id}/browser          — Configure browser source URL/audio (admin only)
- POST /api/streams/{id}/items            — Add asset to playlist (admin or assigned user)
- PUT  /api/streams/{id}/items/reorder    — Reorder playlist items (admin or assigned user)
- DELETE /api/streams/{id}/items/{item_id} — Remove playlist item (admin or assigned user)
- POST /api/streams/{id}/start            — Start streaming (admin or assigned user)
- POST /api/streams/{id}/stop             — Stop streaming (admin or assigned user)
- POST /api/streams/{id}/restart          — Restart streaming (admin or assigned user)
- GET  /api/streams/{id}/status           — Get runtime status (admin or assigned user)

RBAC model:
- Admins can create, configure, delete streams and manage all playlists.
- Regular users can only see/manage streams they've been assigned to.
- Playlist item operations enforce asset ownership: non-admins can only add their own assets.

Two source types:
- PLAYLIST: ffmpeg concat loop of transcoded assets → MPEG-TS multicast
- BROWSER: Podman container with Xvfb + Firefox + ffmpeg x11grab → MPEG-TS multicast
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from backend.database import get_db
from backend.models import (
    Stream, StreamItem, StreamStatus, PlaybackMode, StreamSourceType,
    Asset, AssetStatus, User, UserStreamAssignment, BrowserSource
)
from backend.schemas import (
    StreamCreate, StreamUpdate, StreamResponse, StreamListResponse,
    StreamItemCreate, StreamItemReorder, StreamAssignRequest,
    BrowserSourceConfig,
)
from backend.auth import get_current_user

logger = logging.getLogger("streams")
router = APIRouter(prefix="/api/streams", tags=["streams"])


def _user_can_manage(user: User, stream: Stream) -> bool:
    """Check whether a user has permission to manage a stream.

    Admins can manage all streams. Regular users can only manage
    streams they've been explicitly assigned to via the admin panel.
    """
    if user.is_admin:
        return True
    return any(a.user_id == user.id for a in stream.assigned_users)


def _to_response(stream: Stream) -> dict:
    """Build a stream response dict with nested items and browser source data.

    Items are sorted by position so the frontend renders the playlist
    in the correct order. Each item includes full asset metadata to
    avoid requiring a separate API call per asset.
    """
    items = []
    for item in sorted(stream.items, key=lambda i: i.position):
        a = item.asset
        items.append({
            "id": item.id, "asset_id": item.asset_id, "position": item.position,
            "asset": {
                "id": a.id,
                "display_name": a.display_name or a.original_filename,
                "original_filename": a.original_filename,
                "asset_type": a.asset_type.value, "status": a.status.value,
                "error_message": a.error_message,
                "transcode_progress": a.transcode_progress or 0.0,
                "duration_seconds": a.duration_seconds,
                "width": a.width, "height": a.height,
                "file_size_bytes": a.file_size_bytes,
                "thumbnail_url": f"/api/assets/{a.id}/thumbnail" if a.thumbnail_path else None,
                "preview_url": f"/api/assets/{a.id}/preview" if a.status == AssetStatus.READY else None,
                "owner_id": a.owner_id,
                "owner_name": a.owner.username if a.owner else None,
                "created_at": a.created_at, "updated_at": a.updated_at,
            }})

    # Include browser source config if this is a BROWSER type stream
    browser_data = None
    if stream.browser_source:
        bs = stream.browser_source
        browser_data = {
            "url": bs.url,
            "capture_audio": bs.capture_audio,
            "display_number": bs.display_number,
            "vnc_port": bs.vnc_port,
            "novnc_port": bs.novnc_port,
        }

    return {
        "id": stream.id, "name": stream.name,
        "multicast_address": stream.multicast_address,
        "multicast_port": stream.multicast_port,
        "status": stream.status.value,
        "playback_mode": stream.playback_mode.value,
        "source_type": stream.source_type.value if stream.source_type else "playlist",
        "items": items,
        "browser_source": browser_data,
        "assigned_user_ids": [a.user_id for a in stream.assigned_users],
        "created_at": stream.created_at, "updated_at": stream.updated_at,
    }


# ---------------------------------------------------------------------------
# CRUD (admin only for create/config/delete)
# ---------------------------------------------------------------------------

@router.post("", response_model=StreamResponse, status_code=201)
def create_stream(body: StreamCreate, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    """Create a new stream. Admin only.

    For BROWSER source type, also creates the associated BrowserSource
    record with default values (no URL configured yet). The admin must
    then call PUT /browser to set the URL before starting.
    """
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")

    source_type = StreamSourceType(body.source_type)
    stream = Stream(
        name=body.name,
        multicast_address=body.multicast_address,
        multicast_port=body.multicast_port,
        playback_mode=PlaybackMode(body.playback_mode),
        source_type=source_type,
    )
    db.add(stream)
    # Flush to get stream.id assigned before creating the child BrowserSource
    db.flush()

    if source_type == StreamSourceType.BROWSER:
        db.add(BrowserSource(stream_id=stream.id))

    db.commit()
    db.refresh(stream)
    return _to_response(stream)


@router.get("", response_model=StreamListResponse)
def list_streams(db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    """List streams — admins see all, regular users see only their assigned streams."""
    if current_user.is_admin:
        streams = db.query(Stream).order_by(Stream.created_at.desc()).all()
    else:
        # Filter to only streams this user is assigned to
        assigned_ids = [a.stream_id for a in current_user.assigned_streams]
        streams = db.query(Stream).filter(
            Stream.id.in_(assigned_ids)
        ).order_by(Stream.created_at.desc()).all() if assigned_ids else []
    return StreamListResponse(streams=[_to_response(s) for s in streams],
                              total_count=len(streams))


@router.get("/{stream_id}", response_model=StreamResponse)
def get_stream(stream_id: int, db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    """Get a single stream's details. Requires admin or assignment."""
    stream = db.query(Stream).filter(Stream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")
    if not _user_can_manage(current_user, stream):
        raise HTTPException(status_code=403, detail="Access denied")
    return _to_response(stream)


@router.put("/{stream_id}", response_model=StreamResponse)
def update_stream(stream_id: int, body: StreamUpdate, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    """Update stream configuration (name, multicast address/port, playback mode). Admin only.

    Supports partial updates — only fields that are explicitly provided (not None)
    are changed. Note: changing multicast settings on a running stream requires
    a restart to take effect.
    """
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    stream = db.query(Stream).filter(Stream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")
    if body.name is not None: stream.name = body.name
    if body.multicast_address is not None: stream.multicast_address = body.multicast_address
    if body.multicast_port is not None: stream.multicast_port = body.multicast_port
    if body.playback_mode is not None: stream.playback_mode = PlaybackMode(body.playback_mode)
    db.commit()
    db.refresh(stream)
    return _to_response(stream)


@router.delete("/{stream_id}", status_code=204)
async def delete_stream(stream_id: int, request: Request, db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    """Delete a stream, stopping it first if running. Admin only.

    Dispatches to the appropriate manager (browser_manager or stream_manager)
    based on source type. Cascade deletes remove StreamItems, BrowserSource,
    and UserStreamAssignment records automatically.
    """
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    stream = db.query(Stream).filter(Stream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")

    # Gracefully stop any running source before deleting
    if stream.source_type == StreamSourceType.BROWSER:
        bm = request.app.state.browser_manager
        if bm.is_active(stream_id):
            await bm.stop_browser(stream_id)
    else:
        sm = request.app.state.stream_manager
        if sm.is_stream_active(stream_id):
            await sm.stop_stream(stream_id)

    db.delete(stream)
    db.commit()


# ---------------------------------------------------------------------------
# Channel assignment (admin only)
# ---------------------------------------------------------------------------

@router.put("/{stream_id}/assign", response_model=StreamResponse)
def assign_users(stream_id: int, body: StreamAssignRequest,
                 db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    """Replace the set of users assigned to a stream. Admin only.

    Uses a delete-and-recreate strategy: all existing assignments for
    this stream are removed, then new ones are created for each user ID
    in the request. This simplifies handling additions and removals in
    a single operation.
    """
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    stream = db.query(Stream).filter(Stream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")
    # Validate all user IDs exist before making any changes
    for uid in body.user_ids:
        if not db.query(User).filter(User.id == uid).first():
            raise HTTPException(status_code=404, detail=f"User {uid} not found")
    # Clear existing assignments, then create the new set
    db.query(UserStreamAssignment).filter(
        UserStreamAssignment.stream_id == stream_id
    ).delete()
    for uid in body.user_ids:
        db.add(UserStreamAssignment(user_id=uid, stream_id=stream_id))
    db.commit()
    db.refresh(stream)
    return _to_response(stream)


# ---------------------------------------------------------------------------
# Browser source configuration
# ---------------------------------------------------------------------------

@router.put("/{stream_id}/browser", response_model=StreamResponse)
def update_browser_config(stream_id: int, body: BrowserSourceConfig,
                          db: Session = Depends(get_db),
                          current_user: User = Depends(get_current_user)):
    """Update browser source URL and audio capture setting. Admin only.

    Creates the BrowserSource record if it doesn't exist yet (handles
    edge case of streams created before browser source support was added).
    Changes take effect on next start/restart — a running browser source
    must be restarted to pick up the new URL.
    """
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    stream = db.query(Stream).filter(Stream.id == stream_id).first()
    if not stream or stream.source_type != StreamSourceType.BROWSER:
        raise HTTPException(status_code=400, detail="Not a browser source stream")
    if not stream.browser_source:
        # Lazy-create BrowserSource for streams that predate this feature
        db.add(BrowserSource(stream_id=stream_id, url=body.url,
                             capture_audio=body.capture_audio))
    else:
        stream.browser_source.url = body.url
        stream.browser_source.capture_audio = body.capture_audio
    db.commit()
    db.refresh(stream)
    return _to_response(stream)


# ---------------------------------------------------------------------------
# Playlist management (admin or assigned users)
# ---------------------------------------------------------------------------

@router.post("/{stream_id}/items", response_model=StreamResponse, status_code=201)
def add_item(stream_id: int, body: StreamItemCreate, db: Session = Depends(get_db),
             current_user: User = Depends(get_current_user)):
    """Add an asset to a stream's playlist. Requires admin or stream assignment.

    Non-admin users can only add assets they own, preventing users from
    using other users' content. If no position is specified, the item is
    appended to the end of the playlist.
    """
    stream = db.query(Stream).filter(Stream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")
    if stream.source_type != StreamSourceType.PLAYLIST:
        raise HTTPException(status_code=400, detail="Browser streams don't use playlists")
    if not _user_can_manage(current_user, stream):
        raise HTTPException(status_code=403, detail="Not assigned to this stream")

    asset = db.query(Asset).filter(Asset.id == body.asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    # Ownership check: non-admins can only add their own assets to playlists
    if not current_user.is_admin and asset.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Can only add your own assets")

    # Default position: append after the last item (max position + 1)
    pos = body.position if body.position is not None else max((i.position for i in stream.items), default=-1) + 1
    db.add(StreamItem(stream_id=stream_id, asset_id=body.asset_id, position=pos))
    db.commit()
    db.refresh(stream)
    return _to_response(stream)


@router.put("/{stream_id}/items/reorder", response_model=StreamResponse)
def reorder(stream_id: int, body: StreamItemReorder, db: Session = Depends(get_db),
            current_user: User = Depends(get_current_user)):
    """Reorder playlist items by providing asset IDs in the desired order.

    The frontend sends the complete list of asset IDs in new order.
    Each asset's position is set to its index in the provided list.
    All assets in the request must already be in the playlist.
    """
    stream = db.query(Stream).filter(Stream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")
    if not _user_can_manage(current_user, stream):
        raise HTTPException(status_code=403, detail="Not assigned to this stream")

    # Build a lookup of current items by asset_id for quick validation
    current_items = {i.asset_id: i for i in stream.items}
    for aid in body.asset_ids:
        if aid not in current_items:
            raise HTTPException(status_code=400, detail=f"Asset {aid} not in playlist")
    # Assign new positions based on order in the request
    for pos, aid in enumerate(body.asset_ids):
        current_items[aid].position = pos
    db.commit()
    db.refresh(stream)
    return _to_response(stream)


@router.delete("/{stream_id}/items/{item_id}", status_code=204)
def remove_item(stream_id: int, item_id: int, db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    """Remove an item from a stream's playlist. Requires admin or stream assignment.

    The item is identified by its own ID (not asset ID) to handle cases
    where the same asset appears multiple times in a playlist.
    """
    stream = db.query(Stream).filter(Stream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")
    if not _user_can_manage(current_user, stream):
        raise HTTPException(status_code=403, detail="Not assigned to this stream")
    # Filter by both item_id and stream_id to prevent cross-stream deletion
    item = db.query(StreamItem).filter(StreamItem.id == item_id,
                                       StreamItem.stream_id == stream_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    db.delete(item)
    db.commit()


# ---------------------------------------------------------------------------
# Playback control (admin or assigned users)
# ---------------------------------------------------------------------------

@router.post("/{stream_id}/start")
async def start_stream(stream_id: int, request: Request, db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    """Start a stream. Dispatches to browser_manager or stream_manager based on source type.

    For BROWSER streams: launches a Podman container with Xvfb + Firefox + ffmpeg,
    captures the virtual display, and sends it as MPEG-TS multicast.

    For PLAYLIST streams: starts an ffmpeg concat loop process that reads the
    playlist items and outputs MPEG-TS multicast.

    The stream's DB status is updated to RUNNING on success.
    """
    stream = db.query(Stream).filter(Stream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")
    if not _user_can_manage(current_user, stream):
        raise HTTPException(status_code=403, detail="Not assigned to this stream")

    if stream.source_type == StreamSourceType.BROWSER:
        bm = request.app.state.browser_manager
        if not stream.browser_source or not stream.browser_source.url:
            raise HTTPException(status_code=400, detail="Configure a URL first")
        try:
            result = await bm.start_browser(
                stream_id, stream.browser_source.url,
                stream.browser_source.capture_audio,
                stream.multicast_address, stream.multicast_port,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        stream.status = StreamStatus.RUNNING
        stream.ffmpeg_pid = result.get("ffmpeg_pid")
        db.commit()
        return {"message": f"Browser stream {stream_id} started", "status": "running", **result}
    else:
        # Playlist stream — stream_manager handles status updates internally
        sm = request.app.state.stream_manager
        try:
            await sm.start_stream(stream_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"message": f"Stream {stream_id} started", "status": "running"}


@router.post("/{stream_id}/stop")
async def stop_stream(stream_id: int, request: Request, db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    """Stop a running stream. Dispatches to the appropriate manager.

    For BROWSER streams: stops and removes the Podman container, then
    updates the DB status. For PLAYLIST streams: kills the ffmpeg process
    (stream_manager handles status update internally).
    """
    stream = db.query(Stream).filter(Stream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")
    if not _user_can_manage(current_user, stream):
        raise HTTPException(status_code=403, detail="Not assigned to this stream")

    if stream.source_type == StreamSourceType.BROWSER:
        bm = request.app.state.browser_manager
        await bm.stop_browser(stream_id)
        # Browser manager doesn't update DB status, so we do it here
        stream.status = StreamStatus.STOPPED
        stream.ffmpeg_pid = None
        db.commit()
    else:
        await request.app.state.stream_manager.stop_stream(stream_id)
    return {"message": f"Stream {stream_id} stopped", "status": "stopped"}


@router.post("/{stream_id}/restart")
async def restart_stream(stream_id: int, request: Request, db: Session = Depends(get_db),
                         current_user: User = Depends(get_current_user)):
    """Stop and restart a stream. Handles errors by marking the stream as ERROR.

    This is a convenience endpoint that combines stop + start. If the
    start fails after a successful stop, the stream status is set to
    ERROR so the frontend can display the failure state.
    """
    stream = db.query(Stream).filter(Stream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")
    if not _user_can_manage(current_user, stream):
        raise HTTPException(status_code=403, detail="Not assigned to this stream")

    if stream.source_type == StreamSourceType.BROWSER:
        bm = request.app.state.browser_manager
        await bm.stop_browser(stream_id)
        try:
            await bm.start_browser(
                stream_id, stream.browser_source.url,
                stream.browser_source.capture_audio,
                stream.multicast_address, stream.multicast_port,
            )
            stream.status = StreamStatus.RUNNING
        except ValueError as e:
            # Mark as ERROR so the frontend shows the failure
            stream.status = StreamStatus.ERROR
            db.commit()
            raise HTTPException(status_code=400, detail=str(e))
        db.commit()
    else:
        sm = request.app.state.stream_manager
        await sm.stop_stream(stream_id)
        try:
            await sm.start_stream(stream_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    return {"message": f"Stream {stream_id} restarted", "status": "running"}


@router.get("/{stream_id}/status")
def stream_status(stream_id: int, request: Request, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    """Get a stream's runtime status from the appropriate manager.

    Returns both the DB-persisted status and the live runtime info
    (PID, uptime, etc.) from the stream or browser manager. The frontend
    uses this to show real-time stream health indicators.
    """
    stream = db.query(Stream).filter(Stream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")
    if not _user_can_manage(current_user, stream):
        raise HTTPException(status_code=403, detail="Not assigned to this stream")

    if stream.source_type == StreamSourceType.BROWSER:
        bm = request.app.state.browser_manager
        return {"stream_id": stream_id, "db_status": stream.status.value,
                "runtime": bm.get_status(stream_id)}
    else:
        return {"stream_id": stream_id, "db_status": stream.status.value,
                "runtime": request.app.state.stream_manager.get_status(stream_id)}
