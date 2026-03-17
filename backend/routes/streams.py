"""Stream routes with RBAC and browser source support."""

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
    if user.is_admin:
        return True
    return any(a.user_id == user.id for a in stream.assigned_users)


def _to_response(stream: Stream) -> dict:
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


# --- CRUD (admin only for create/config/delete) ---

@router.post("", response_model=StreamResponse, status_code=201)
def create_stream(body: StreamCreate, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
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
    db.flush()  # Get stream.id before creating browser source

    # If browser type, create the associated BrowserSource record
    if source_type == StreamSourceType.BROWSER:
        db.add(BrowserSource(stream_id=stream.id))

    db.commit()
    db.refresh(stream)
    return _to_response(stream)


@router.get("", response_model=StreamListResponse)
def list_streams(db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    if current_user.is_admin:
        streams = db.query(Stream).order_by(Stream.created_at.desc()).all()
    else:
        assigned_ids = [a.stream_id for a in current_user.assigned_streams]
        streams = db.query(Stream).filter(
            Stream.id.in_(assigned_ids)
        ).order_by(Stream.created_at.desc()).all() if assigned_ids else []
    return StreamListResponse(streams=[_to_response(s) for s in streams],
                              total_count=len(streams))


@router.get("/{stream_id}", response_model=StreamResponse)
def get_stream(stream_id: int, db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    stream = db.query(Stream).filter(Stream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")
    if not _user_can_manage(current_user, stream):
        raise HTTPException(status_code=403, detail="Access denied")
    return _to_response(stream)


@router.put("/{stream_id}", response_model=StreamResponse)
def update_stream(stream_id: int, body: StreamUpdate, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
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
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    stream = db.query(Stream).filter(Stream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")

    # Stop any running source
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


# --- Channel assignment (admin only) ---

@router.put("/{stream_id}/assign", response_model=StreamResponse)
def assign_users(stream_id: int, body: StreamAssignRequest,
                 db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    stream = db.query(Stream).filter(Stream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")
    for uid in body.user_ids:
        if not db.query(User).filter(User.id == uid).first():
            raise HTTPException(status_code=404, detail=f"User {uid} not found")
    db.query(UserStreamAssignment).filter(
        UserStreamAssignment.stream_id == stream_id
    ).delete()
    for uid in body.user_ids:
        db.add(UserStreamAssignment(user_id=uid, stream_id=stream_id))
    db.commit()
    db.refresh(stream)
    return _to_response(stream)


# --- Browser source config ---

@router.put("/{stream_id}/browser", response_model=StreamResponse)
def update_browser_config(stream_id: int, body: BrowserSourceConfig,
                          db: Session = Depends(get_db),
                          current_user: User = Depends(get_current_user)):
    """Update browser source URL and audio settings. Admin only."""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    stream = db.query(Stream).filter(Stream.id == stream_id).first()
    if not stream or stream.source_type != StreamSourceType.BROWSER:
        raise HTTPException(status_code=400, detail="Not a browser source stream")
    if not stream.browser_source:
        db.add(BrowserSource(stream_id=stream_id, url=body.url,
                             capture_audio=body.capture_audio))
    else:
        stream.browser_source.url = body.url
        stream.browser_source.capture_audio = body.capture_audio
    db.commit()
    db.refresh(stream)
    return _to_response(stream)


# --- Playlist management (admin or assigned) ---

@router.post("/{stream_id}/items", response_model=StreamResponse, status_code=201)
def add_item(stream_id: int, body: StreamItemCreate, db: Session = Depends(get_db),
             current_user: User = Depends(get_current_user)):
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
    if not current_user.is_admin and asset.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Can only add your own assets")
    pos = body.position if body.position is not None else max((i.position for i in stream.items), default=-1) + 1
    db.add(StreamItem(stream_id=stream_id, asset_id=body.asset_id, position=pos))
    db.commit()
    db.refresh(stream)
    return _to_response(stream)


@router.put("/{stream_id}/items/reorder", response_model=StreamResponse)
def reorder(stream_id: int, body: StreamItemReorder, db: Session = Depends(get_db),
            current_user: User = Depends(get_current_user)):
    stream = db.query(Stream).filter(Stream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")
    if not _user_can_manage(current_user, stream):
        raise HTTPException(status_code=403, detail="Not assigned to this stream")
    current_items = {i.asset_id: i for i in stream.items}
    for aid in body.asset_ids:
        if aid not in current_items:
            raise HTTPException(status_code=400, detail=f"Asset {aid} not in playlist")
    for pos, aid in enumerate(body.asset_ids):
        current_items[aid].position = pos
    db.commit()
    db.refresh(stream)
    return _to_response(stream)


@router.delete("/{stream_id}/items/{item_id}", status_code=204)
def remove_item(stream_id: int, item_id: int, db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    stream = db.query(Stream).filter(Stream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")
    if not _user_can_manage(current_user, stream):
        raise HTTPException(status_code=403, detail="Not assigned to this stream")
    item = db.query(StreamItem).filter(StreamItem.id == item_id,
                                       StreamItem.stream_id == stream_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    db.delete(item)
    db.commit()


# --- Playback control ---

@router.post("/{stream_id}/start")
async def start_stream(stream_id: int, request: Request, db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
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
        sm = request.app.state.stream_manager
        try:
            await sm.start_stream(stream_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"message": f"Stream {stream_id} started", "status": "running"}


@router.post("/{stream_id}/stop")
async def stop_stream(stream_id: int, request: Request, db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    stream = db.query(Stream).filter(Stream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")
    if not _user_can_manage(current_user, stream):
        raise HTTPException(status_code=403, detail="Not assigned to this stream")

    if stream.source_type == StreamSourceType.BROWSER:
        bm = request.app.state.browser_manager
        await bm.stop_browser(stream_id)
        stream.status = StreamStatus.STOPPED
        stream.ffmpeg_pid = None
        db.commit()
    else:
        await request.app.state.stream_manager.stop_stream(stream_id)
    return {"message": f"Stream {stream_id} stopped", "status": "stopped"}


@router.post("/{stream_id}/restart")
async def restart_stream(stream_id: int, request: Request, db: Session = Depends(get_db),
                         current_user: User = Depends(get_current_user)):
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
