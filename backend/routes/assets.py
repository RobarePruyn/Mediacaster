"""Asset routes with ownership-based visibility and storage info."""

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
    return AssetResponse(
        id=asset.id,
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
    user_filename = file.filename or "unknown"
    ext = Path(user_filename).suffix.lower()
    if ext not in ALL_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type '{ext}'")
    asset_type = classify_upload(user_filename)
    unique_name = f"{uuid.uuid4().hex}{ext}"
    upload_path = UPLOAD_DIR / unique_name

    try:
        with open(upload_path, "wb") as dest:
            while chunk := await file.read(1024 * 1024):
                dest.write(chunk)
    except Exception as exc:
        if upload_path.exists():
            os.remove(upload_path)
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
    background_tasks.add_task(transcode_asset, asset.id, SessionLocal)
    return _to_response(asset)


@router.get("", response_model=AssetListResponse)
def list_assets(db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    """List assets — admins see all, regular users see only their own."""
    if current_user.is_admin:
        assets = db.query(Asset).order_by(Asset.created_at.desc()).all()
    else:
        assets = db.query(Asset).filter(
            Asset.owner_id == current_user.id
        ).order_by(Asset.created_at.desc()).all()
    return AssetListResponse(assets=[_to_response(a) for a in assets],
                             total_count=len(assets))


@router.get("/storage", response_model=StorageResponse)
def get_storage(current_user: User = Depends(get_current_user)):
    """Get storage space info — available to all users."""
    media_path = str(MEDIA_DIR)
    disk = psutil.disk_usage(media_path)
    total_gb = round(disk.total / (1024 ** 3), 2)
    used_gb = round(disk.used / (1024 ** 3), 2)
    available_gb = round(disk.free / (1024 ** 3), 2)
    usable_gb = round(total_gb * 0.8, 2)                    # 80% of total
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
    asset = db.query(Asset).filter(Asset.id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    if not current_user.is_admin and asset.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    for fp in [asset.file_path, asset.thumbnail_path]:
        if fp and os.path.exists(fp):
            try:
                os.remove(fp)
            except OSError:
                pass
    db.delete(asset)
    db.commit()


@router.get("/{asset_id}/thumbnail")
def serve_thumbnail(asset_id: int, db: Session = Depends(get_db)):
    asset = db.query(Asset).filter(Asset.id == asset_id).first()
    if not asset or not asset.thumbnail_path or not os.path.exists(asset.thumbnail_path):
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return FileResponse(asset.thumbnail_path, media_type="image/jpeg")


@router.get("/{asset_id}/preview")
def serve_preview(asset_id: int, db: Session = Depends(get_db)):
    asset = db.query(Asset).filter(Asset.id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    if asset.status != AssetStatus.READY:
        raise HTTPException(status_code=409, detail=f"Not ready ({asset.status.value})")
    if not os.path.exists(asset.file_path):
        raise HTTPException(status_code=404, detail="File missing from disk")
    return FileResponse(asset.file_path, media_type="video/mp4")
