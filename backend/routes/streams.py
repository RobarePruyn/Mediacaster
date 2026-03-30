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

Three source types:
- PLAYLIST: ffmpeg concat loop of transcoded assets → MPEG-TS multicast
- BROWSER: Wayland capture (weston + Firefox + wf-recorder + ffmpeg → MPEG-TS multicast)
- PRESENTATION: Wayland capture (weston + LibreOffice Impress + wf-recorder + ffmpeg → MPEG-TS multicast)
"""

import logging
import os
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from backend.database import get_db
from backend.models import (
    Stream, StreamItem, StreamStatus, PlaybackMode, StreamSourceType,
    Asset, AssetStatus, User, UserStreamAssignment, BrowserSource,
    Presentation, PresentationStatus,
)
from backend.schemas import (
    StreamCreate, StreamUpdate, StreamResponse, StreamListResponse,
    StreamItemCreate, StreamItemReorder, StreamAssignRequest,
    BrowserSourceConfig,
)
from backend.services.encoding_profiles import (
    get_effective_bitrate, get_effective_gop_size, validate_stream_profile,
    estimate_cpu_cost,
)
from backend.services.monitor import get_system_stats
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
    remote_control = None
    if stream.browser_source:
        bs = stream.browser_source
        browser_data = {
            "url": bs.url,
            "capture_audio": bs.capture_audio,
            "display_number": bs.display_number,
            "vnc_port": bs.vnc_port,
            "novnc_port": bs.novnc_port,
            "presentation_id": bs.presentation_id,
        }
        # If the browser source is linked to a ready presentation, include
        # remote control info so the frontend can show slide navigation controls
        if bs.presentation_id and bs.presentation:
            pres = bs.presentation
            if pres.status == PresentationStatus.READY:
                remote_control = {
                    "type": "presentation",
                    "presentation_id": pres.id,
                    "current_slide": pres.current_slide or 1,
                    "total_slides": pres.slide_count or 0,
                    "presentation_name": pres.name,
                }

    return {
        "id": stream.id, "name": stream.name,
        "multicast_address": stream.multicast_address,
        "multicast_port": stream.multicast_port,
        "status": stream.status.value,
        "playback_mode": stream.playback_mode.value,
        "source_type": stream.source_type.value if stream.source_type else "playlist",
        # Per-stream encoding profile
        "resolution": stream.resolution or "1920x1080",
        "codec": stream.codec or "h264",
        "framerate": stream.framerate or 30,
        "video_bitrate": stream.video_bitrate,
        "effective_bitrate": get_effective_bitrate(
            stream.resolution or "1920x1080",
            stream.framerate or 30,
            stream.codec or "h264",
            stream.video_bitrate,
        ),
        "gop_size": stream.gop_size,
        "effective_gop_size": get_effective_gop_size(
            stream.framerate or 30, stream.gop_size,
        ),
        "items": items,
        "browser_source": browser_data,
        "remote_control": remote_control,
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

    # Auto-assign next available multicast address if none provided.
    # Starts at 239.1.1.1 and increments the last octet, wrapping through
    # the third and second octets within the 239.0.0.0/8 admin-scoped range.
    if not body.multicast_address:
        base_parts = [239, 1, 1, 1]
        used_addresses = {
            s.multicast_address
            for s in db.query(Stream.multicast_address).filter(
                Stream.multicast_port == body.multicast_port
            ).all()
        }
        found = False
        # Search up to 65025 addresses (255*255*1) in the 239.x.x.x range
        for _ in range(255 * 255):
            candidate = ".".join(str(o) for o in base_parts)
            if candidate not in used_addresses:
                body.multicast_address = candidate
                found = True
                break
            # Increment: last octet, then third, then second
            base_parts[3] += 1
            if base_parts[3] > 254:
                base_parts[3] = 1
                base_parts[2] += 1
                if base_parts[2] > 254:
                    base_parts[2] = 1
                    base_parts[1] += 1
                    if base_parts[1] > 254:
                        break
        if not found:
            raise HTTPException(status_code=409, detail="No available multicast addresses")

    # Prevent two streams from sharing the same multicast address:port —
    # two ffmpeg processes writing to the same group corrupt each other's output
    conflict = db.query(Stream).filter(
        Stream.multicast_address == body.multicast_address,
        Stream.multicast_port == body.multicast_port,
    ).first()
    if conflict:
        raise HTTPException(
            status_code=409,
            detail=f"Multicast {body.multicast_address}:{body.multicast_port} "
                   f"is already in use by stream '{conflict.name}'",
        )

    source_type = StreamSourceType(body.source_type)

    # Validate encoding profile constraints
    profile_error = validate_stream_profile(
        body.source_type, body.resolution, body.codec, body.framerate,
    )
    if profile_error:
        raise HTTPException(status_code=400, detail=profile_error)

    stream = Stream(
        name=body.name,
        multicast_address=body.multicast_address,
        multicast_port=body.multicast_port,
        playback_mode=PlaybackMode(body.playback_mode),
        source_type=source_type,
        resolution=body.resolution,
        codec=body.codec,
        framerate=body.framerate,
        video_bitrate=body.video_bitrate,
        gop_size=body.gop_size,
    )
    db.add(stream)
    # Flush to get stream.id assigned before creating the child BrowserSource
    db.flush()

    # Browser and Presentation streams both need a BrowserSource record
    # to track display/VNC/noVNC port assignments and (for presentations)
    # the linked presentation_id
    if source_type in (StreamSourceType.BROWSER, StreamSourceType.PRESENTATION):
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
    # Per-stream encoding profile updates
    if body.resolution is not None: stream.resolution = body.resolution
    if body.codec is not None: stream.codec = body.codec
    if body.framerate is not None: stream.framerate = body.framerate
    # Allow explicit null to clear overrides (revert to auto-defaults)
    if "video_bitrate" in (body.model_fields_set if hasattr(body, 'model_fields_set') else set()):
        stream.video_bitrate = body.video_bitrate
    if "gop_size" in (body.model_fields_set if hasattr(body, 'model_fields_set') else set()):
        stream.gop_size = body.gop_size

    # Validate encoding profile after applying changes
    profile_error = validate_stream_profile(
        stream.source_type.value if stream.source_type else "playlist",
        stream.resolution, stream.codec, stream.framerate,
    )
    if profile_error:
        raise HTTPException(status_code=400, detail=profile_error)

    # If multicast address or port changed, check for conflicts with other streams
    conflict = db.query(Stream).filter(
        Stream.multicast_address == stream.multicast_address,
        Stream.multicast_port == stream.multicast_port,
        Stream.id != stream.id,
    ).first()
    if conflict:
        raise HTTPException(
            status_code=409,
            detail=f"Multicast {stream.multicast_address}:{stream.multicast_port} "
                   f"is already in use by stream '{conflict.name}'",
        )

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
    if stream.source_type in (StreamSourceType.BROWSER, StreamSourceType.PRESENTATION):
        bm = request.app.state.browser_manager
        if bm.is_active(stream_id):
            await bm.stop_browser(stream_id)
    elif stream.source_type == StreamSourceType.PLAYLIST:
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
    if not stream or stream.source_type not in (StreamSourceType.BROWSER, StreamSourceType.PRESENTATION):
        raise HTTPException(status_code=400, detail="Not a browser or presentation source stream")

    url = body.url or "about:blank"
    presentation_id = body.presentation_id

    # Validate presentation exists if one is specified
    if presentation_id is not None:
        pres = db.query(Presentation).filter(Presentation.id == presentation_id).first()
        if not pres:
            raise HTTPException(status_code=404, detail="Presentation not found")

    if not stream.browser_source:
        # Lazy-create BrowserSource for streams that predate this feature
        db.add(BrowserSource(stream_id=stream_id, url=url,
                             capture_audio=body.capture_audio,
                             presentation_id=presentation_id))
    else:
        stream.browser_source.url = url
        stream.browser_source.capture_audio = body.capture_audio
        stream.browser_source.presentation_id = presentation_id
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
        raise HTTPException(status_code=400, detail="Only playlist streams support playlist items")
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

    For BROWSER/PRESENTATION streams: launches a native Wayland capture pipeline
    (weston + Firefox/LibreOffice + wf-recorder + ffmpeg → MPEG-TS multicast).

    For PLAYLIST streams: starts an ffmpeg concat loop process that reads the
    playlist items and outputs MPEG-TS multicast.

    The stream's DB status is updated to RUNNING on success.
    """
    stream = db.query(Stream).filter(Stream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")
    if not _user_can_manage(current_user, stream):
        raise HTTPException(status_code=403, detail="Not assigned to this stream")

    # Pre-start capacity check: estimate whether the server has enough CPU headroom
    # for this stream's encoding profile. Only a warning — does not block start.
    capacity_warning = None
    try:
        estimated_cost = estimate_cpu_cost(
            stream.source_type.value if hasattr(stream.source_type, "value") else str(stream.source_type),
            stream.resolution or "1920x1080",
            stream.framerate or 30,
            stream.codec or "h264",
        )
        system_stats = get_system_stats()
        current_cpu = system_stats["cpu_percent"]
        # Warn if the estimated total would exceed 80% of system CPU
        if current_cpu + estimated_cost > 80.0:
            capacity_warning = (
                f"CPU headroom warning: system at {current_cpu:.0f}%, "
                f"this stream estimated to add ~{estimated_cost:.0f}%. "
                f"Quality may degrade."
            )
            logger.warning("Capacity warning for stream %d: %s", stream_id, capacity_warning)
    except Exception as exc:
        logger.debug("Capacity check failed (non-fatal): %s", exc)

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
        resp = {"message": f"Browser stream {stream_id} started", "status": "running", **result}
        if capacity_warning:
            resp["capacity_warning"] = capacity_warning
        return resp

    elif stream.source_type == StreamSourceType.PRESENTATION:
        bm = request.app.state.browser_manager
        # Presentation streams need a linked presentation with a file on disk
        if not stream.browser_source or not stream.browser_source.presentation_id:
            raise HTTPException(status_code=400, detail="Select a presentation first")
        presentation = db.query(Presentation).filter(
            Presentation.id == stream.browser_source.presentation_id
        ).first()
        if not presentation or not presentation.file_path:
            raise HTTPException(status_code=400, detail="Presentation file not available")
        if not os.path.isfile(presentation.file_path):
            raise HTTPException(status_code=400,
                                detail=f"Presentation file missing from disk. "
                                       f"Please re-upload the presentation.")
        if presentation.status != PresentationStatus.READY:
            raise HTTPException(status_code=400,
                                detail=f"Presentation is not ready (status: {presentation.status.value})")
        try:
            result = await bm.start_presentation(
                stream_id, presentation.file_path,
                stream.browser_source.capture_audio,
                stream.multicast_address, stream.multicast_port,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        stream.status = StreamStatus.RUNNING
        db.commit()
        resp = {"message": f"Presentation stream {stream_id} started", "status": "running", **result}
        if capacity_warning:
            resp["capacity_warning"] = capacity_warning
        return resp

    else:
        # Playlist stream — stream_manager handles status updates internally
        sm = request.app.state.stream_manager
        try:
            await sm.start_stream(stream_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        resp = {"message": f"Stream {stream_id} started", "status": "running"}
        if capacity_warning:
            resp["capacity_warning"] = capacity_warning
        return resp


@router.post("/{stream_id}/stop")
async def stop_stream(stream_id: int, request: Request, db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    """Stop a running stream. Dispatches to the appropriate manager.

    For BROWSER/PRESENTATION streams: terminates all Wayland capture processes,
    then updates the DB status. For PLAYLIST streams: kills the ffmpeg process
    (stream_manager handles status update internally).
    """
    stream = db.query(Stream).filter(Stream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")
    if not _user_can_manage(current_user, stream):
        raise HTTPException(status_code=403, detail="Not assigned to this stream")

    if stream.source_type in (StreamSourceType.BROWSER, StreamSourceType.PRESENTATION):
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

    if stream.source_type in (StreamSourceType.BROWSER, StreamSourceType.PRESENTATION):
        bm = request.app.state.browser_manager
        await bm.stop_browser(stream_id)
        try:
            if stream.source_type == StreamSourceType.PRESENTATION:
                # Re-resolve the presentation file for restart
                presentation = db.query(Presentation).filter(
                    Presentation.id == stream.browser_source.presentation_id
                ).first()
                if not presentation or not presentation.file_path:
                    raise ValueError("Presentation file not available")
                if not os.path.isfile(presentation.file_path):
                    raise ValueError("Presentation file missing from disk. Please re-upload.")
                await bm.start_presentation(
                    stream_id, presentation.file_path,
                    stream.browser_source.capture_audio,
                    stream.multicast_address, stream.multicast_port,
                )
            else:
                await bm.start_browser(
                    stream_id, stream.browser_source.url,
                    stream.browser_source.capture_audio,
                    stream.multicast_address, stream.multicast_port,
                )
            stream.status = StreamStatus.RUNNING
        except ValueError as e:
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

    if stream.source_type in (StreamSourceType.BROWSER, StreamSourceType.PRESENTATION):
        bm = request.app.state.browser_manager
        return {"stream_id": stream_id, "db_status": stream.status.value,
                "runtime": bm.get_status(stream_id)}
    else:
        return {"stream_id": stream_id, "db_status": stream.status.value,
                "runtime": request.app.state.stream_manager.get_status(stream_id)}


# ---------------------------------------------------------------------------
# Presentation slide control (admin or assigned users)
# ---------------------------------------------------------------------------

@router.post("/{stream_id}/slide-control")
async def slide_control(stream_id: int, body: dict, request: Request,
                        db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    """Send a slide navigation command to a running presentation stream.

    Sends ydotool key events to the LibreOffice Impress instance via the
    Wayland compositor. Supported actions map to LibreOffice slideshow shortcuts:

      next:  Right arrow — advance to next slide/animation
      prev:  Left arrow — go back one slide/animation
      first: Home — jump to first slide
      last:  End — jump to last slide

    Only works on PRESENTATION source type streams that are currently running.
    """
    stream = db.query(Stream).filter(Stream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")
    if not _user_can_manage(current_user, stream):
        raise HTTPException(status_code=403, detail="Not assigned to this stream")
    if stream.source_type != StreamSourceType.PRESENTATION:
        raise HTTPException(status_code=400, detail="Not a presentation stream")
    if stream.status != StreamStatus.RUNNING:
        raise HTTPException(status_code=400, detail="Stream is not running")

    # Map action names to X11 keysym names for xdotool
    action = body.get("action")
    key_map = {
        "next": "Right",
        "prev": "Left",
        "first": "Home",
        "last": "End",
    }
    key = key_map.get(action)
    if not key:
        raise HTTPException(status_code=400,
                            detail=f"Invalid action '{action}'. Use: next, prev, first, last")

    bm = request.app.state.browser_manager
    success = await bm.send_key(stream_id, key)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to send key command to capture source")

    return {"message": f"Slide control '{action}' sent", "action": action}
