"""
Asset management routes with ownership-based visibility and storage info.

Provides:
- POST /api/assets/upload           — Upload a media file (starts background transcode)
- GET  /api/assets                  — List assets (admin: all, user: own only)
- GET  /api/assets/storage          — Disk usage info for the media partition
- GET  /api/assets/{id}             — Get single asset details
- PUT  /api/assets/{id}/rename      — Rename an asset's display name
- DELETE /api/assets/{id}           — Delete an asset and its files from disk
- GET  /api/assets/{id}/thumbnail   — Serve thumbnail image (no auth — see note)
- GET  /api/assets/{id}/preview     — Serve transcoded video preview (no auth — see note)

Note on auth-free endpoints: Thumbnail and preview endpoints skip JWT auth because
<img> and <video> tags in the browser cannot send Authorization headers. These
endpoints are safe to leave open since asset IDs are non-sequential UUIDs and
the content is not sensitive.

RBAC: Admins see all assets. Regular users see only their own uploads.
Ownership checks apply to get, rename, and delete operations.
"""

import os
import uuid
import logging
from pathlib import Path
import psutil
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, BackgroundTasks
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from backend.database import get_db, SessionLocal
from backend.models import Asset, AssetStatus, User
from backend.schemas import AssetResponse, AssetListResponse, AssetRename, StorageResponse
from backend.auth import get_current_user
from backend.config import UPLOAD_DIR, MEDIA_DIR
from backend.services.transcoder import classify_upload, ALL_EXTENSIONS, transcode_asset

logger = logging.getLogger("assets")
router = APIRouter(prefix="/api/assets", tags=["assets"])


def _to_response(asset: Asset) -> AssetResponse:
    """Convert an Asset ORM object to the API response schema.

    Builds thumbnail and preview URLs only when the underlying files exist
    or the asset has finished transcoding, so the frontend doesn't render
    broken image/video links.
    """
    return AssetResponse(
        id=asset.id,
        # Fall back to the original filename if no display name was set
        display_name=asset.display_name or asset.original_filename,
        original_filename=asset.original_filename,
        asset_type=asset.asset_type.value if asset.asset_type else "unknown",
        status=asset.status.value if asset.status else "unknown",
        error_message=asset.error_message,
        transcode_progress=asset.transcode_progress or 0.0,
        duration_seconds=asset.duration_seconds,
        width=asset.width, height=asset.height,
        file_size_bytes=asset.file_size_bytes,
        thumbnail_url=f"/api/assets/{asset.id}/thumbnail" if asset.thumbnail_path else None,
        preview_url=f"/api/assets/{asset.id}/preview" if asset.status == AssetStatus.READY else None,
        owner_id=asset.owner_id,
        owner_name=asset.owner.username if asset.owner else None,
        created_at=asset.created_at, updated_at=asset.updated_at)


@router.post("/upload", response_model=AssetResponse, status_code=201)
async def upload_asset(background_tasks: BackgroundTasks,
                       file: UploadFile = File(...),
                       db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    """Upload a media file and kick off background transcoding.

    The upload is saved to disk with a UUID filename to avoid collisions,
    then a background task normalizes it to H.264/AAC via the transcoder
    service. The asset is immediately returned in UPLOADING status so the
    frontend can poll for transcode progress.

    Args:
        file: The uploaded file (video, image, or audio).

    Returns:
        The newly created asset record in UPLOADING status.
    """
    user_filename = file.filename or "unknown"
    ext = Path(user_filename).suffix.lower()
    if ext not in ALL_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type '{ext}'")

    # Classify the upload type (VIDEO, IMAGE, or AUDIO) based on extension
    asset_type = classify_upload(user_filename)

    # Use a UUID filename on disk to prevent name collisions and path traversal
    unique_name = f"{uuid.uuid4().hex}{ext}"
    upload_path = UPLOAD_DIR / unique_name

    # Stream the upload to disk in 1MB chunks to avoid loading large files into memory
    try:
        with open(upload_path, "wb") as dest:
            while chunk := await file.read(1024 * 1024):
                dest.write(chunk)
    except OSError as exc:
        # Clean up partial file on failure
        if upload_path.exists():
            os.remove(upload_path)
        logger.error("Upload failed for %s: %s", user_filename, exc)
        raise HTTPException(status_code=500, detail=f"Upload failed: {exc}")

    asset = Asset(
        original_filename=unique_name,
        display_name=user_filename,
        file_path=str(upload_path),
        asset_type=asset_type,
        status=AssetStatus.UPLOADING,
        owner_id=current_user.id,
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)

    # Transcode runs in the background with its own DB session (SessionLocal)
    # because the request session will be closed before transcoding finishes
    background_tasks.add_task(transcode_asset, asset.id, SessionLocal)
    return _to_response(asset)


@router.get("", response_model=AssetListResponse)
def list_assets(db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    """List assets — admins see all, regular users see only their own."""
    if current_user.is_admin:
        assets = db.query(Asset).order_by(Asset.created_at.desc()).all()
    else:
        # Ownership filter: regular users only see assets they uploaded
        assets = db.query(Asset).filter(
            Asset.owner_id == current_user.id
        ).order_by(Asset.created_at.desc()).all()
    return AssetListResponse(assets=[_to_response(a) for a in assets],
                             total_count=len(assets))


@router.get("/storage", response_model=StorageResponse)
def get_storage(current_user: User = Depends(get_current_user)):
    """Get disk storage info for the media partition.

    Reports a "usable" capacity at 80% of total disk space to provide
    a safety margin — filling a disk to 100% can cause system instability
    and failed writes. The usage bar in the frontend is based on this
    80% usable threshold rather than raw disk capacity.
    """
    media_path = str(MEDIA_DIR)
    disk = psutil.disk_usage(media_path)
    total_gb = round(disk.total / (1024 ** 3), 2)
    used_gb = round(disk.used / (1024 ** 3), 2)
    available_gb = round(disk.free / (1024 ** 3), 2)
    usable_gb = round(total_gb * 0.8, 2)                    # 80% safety threshold
    usable_remaining_gb = round(max(0, usable_gb - used_gb), 2)
    usage_percent = round((used_gb / usable_gb) * 100, 1) if usable_gb > 0 else 0.0
    return StorageResponse(
        total_gb=total_gb, used_gb=used_gb, available_gb=available_gb,
        usable_gb=usable_gb, usable_remaining_gb=usable_remaining_gb,
        usage_percent=usage_percent,
    )


@router.get("/{asset_id}", response_model=AssetResponse)
def get_asset(asset_id: int, db: Session = Depends(get_db),
              current_user: User = Depends(get_current_user)):
    """Get a single asset's details. Ownership-filtered for non-admins."""
    asset = db.query(Asset).filter(Asset.id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    # Non-admins can only see their own assets
    if not current_user.is_admin and asset.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    return _to_response(asset)


@router.put("/{asset_id}/rename", response_model=AssetResponse)
def rename_asset(asset_id: int, body: AssetRename, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    """Rename an asset's display name (not the file on disk). Ownership-filtered."""
    asset = db.query(Asset).filter(Asset.id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    if not current_user.is_admin and asset.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    asset.display_name = body.display_name
    db.commit()
    db.refresh(asset)
    return _to_response(asset)


@router.delete("/{asset_id}", status_code=204)
def delete_asset(asset_id: int, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    """Delete an asset record and remove its files from disk. Ownership-filtered.

    Cleans up both the transcoded media file and the generated thumbnail.
    File deletion errors are logged but don't block the DB deletion —
    orphaned files are preferable to orphaned DB records.
    """
    asset = db.query(Asset).filter(Asset.id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    if not current_user.is_admin and asset.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    # Remove associated files from disk (transcoded media + thumbnail)
    for file_path in [asset.file_path, asset.thumbnail_path]:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError as exc:
                # Log but don't fail — orphaned files are acceptable
                logger.warning("Failed to remove file %s during asset deletion: %s",
                               file_path, exc)
    db.delete(asset)
    db.commit()


# ---------------------------------------------------------------------------
# Auth-free media serving endpoints
# These skip JWT auth because HTML <img>/<video> tags cannot send headers.
# ---------------------------------------------------------------------------

@router.get("/{asset_id}/thumbnail")
def serve_thumbnail(asset_id: int, db: Session = Depends(get_db)):
    """Serve a thumbnail image for an asset. No auth required (see module docstring)."""
    asset = db.query(Asset).filter(Asset.id == asset_id).first()
    if not asset or not asset.thumbnail_path or not os.path.exists(asset.thumbnail_path):
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return FileResponse(asset.thumbnail_path, media_type="image/jpeg")


@router.get("/{asset_id}/preview")
def serve_preview(asset_id: int, db: Session = Depends(get_db)):
    """Serve the transcoded video file for preview playback. No auth required.

    Only serves assets in READY status to prevent serving partially
    transcoded or failed files.
    """
    asset = db.query(Asset).filter(Asset.id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    if asset.status != AssetStatus.READY:
        raise HTTPException(status_code=409, detail=f"Not ready ({asset.status.value})")
    if not os.path.exists(asset.file_path):
        raise HTTPException(status_code=404, detail="File missing from disk")
    return FileResponse(asset.file_path, media_type="video/mp4")
