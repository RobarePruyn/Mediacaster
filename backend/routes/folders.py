"""
Folder management routes for organizing media assets into nested directories.

Provides:
- POST   /api/folders              — Create a new folder
- GET    /api/folders              — List folders visible to the current user
- GET    /api/folders/tree         — Get the full folder tree (nested structure)
- GET    /api/folders/{id}         — Get a single folder with its assets
- PUT    /api/folders/{id}         — Rename or move a folder
- PUT    /api/folders/{id}/share   — Set sharing options (admin only)
- DELETE /api/folders/{id}         — Delete a folder (assets become unfiled)
- POST   /api/folders/move-assets  — Move assets into a folder (or unfiled)

Visibility rules:
  - Admins see all folders
  - Regular users see folders they own + shared folders
  - Sharing is toggleable per-folder with read-only or read-write modes
  - Only admins can set folder sharing
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from backend.database import get_db
from backend.models import Folder, FolderShareMode, Asset, User
from backend.schemas import (
    FolderCreate, FolderUpdate, FolderShareUpdate,
    FolderResponse, FolderTreeResponse, MoveAssetsRequest,
)
from backend.auth import get_current_user

logger = logging.getLogger("folders")
router = APIRouter(prefix="/api/folders", tags=["folders"])


def _to_response(folder: Folder, db: Session) -> FolderResponse:
    """Convert a Folder ORM object to the API response schema."""
    asset_count = db.query(func.count(Asset.id)).filter(Asset.folder_id == folder.id).scalar()
    child_count = db.query(func.count(Folder.id)).filter(Folder.parent_id == folder.id).scalar()
    return FolderResponse(
        id=folder.id,
        name=folder.name,
        parent_id=folder.parent_id,
        owner_id=folder.owner_id,
        owner_name=folder.owner.username if folder.owner else None,
        is_shared=folder.is_shared,
        share_mode=folder.share_mode.value if folder.share_mode else "read_only",
        asset_count=asset_count,
        child_count=child_count,
        created_at=folder.created_at,
        updated_at=folder.updated_at,
    )


def _visible_folders_query(db: Session, user: User):
    """Build a query for folders visible to the given user."""
    if user.is_admin:
        return db.query(Folder)
    # Regular users: own folders + shared folders
    return db.query(Folder).filter(
        (Folder.owner_id == user.id) | (Folder.is_shared == True)
    )


def _check_folder_write_access(folder: Folder, user: User):
    """Raise 403 if the user doesn't have write access to this folder."""
    if user.is_admin:
        return
    if folder.owner_id == user.id:
        return
    if folder.is_shared and folder.share_mode == FolderShareMode.READ_WRITE:
        return
    raise HTTPException(status_code=403, detail="Access denied")


def _check_folder_read_access(folder: Folder, user: User):
    """Raise 403 if the user can't even view this folder."""
    if user.is_admin:
        return
    if folder.owner_id == user.id:
        return
    if folder.is_shared:
        return
    raise HTTPException(status_code=403, detail="Access denied")


def _would_create_cycle(db: Session, folder_id: int, new_parent_id: int) -> bool:
    """Check if moving folder_id under new_parent_id would create a cycle."""
    current = new_parent_id
    while current is not None:
        if current == folder_id:
            return True
        parent = db.query(Folder).filter(Folder.id == current).first()
        current = parent.parent_id if parent else None
    return False


@router.post("", response_model=FolderResponse, status_code=201)
def create_folder(body: FolderCreate, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    """Create a new folder. Any user can create folders."""
    # Validate parent exists and user has access to it
    if body.parent_id is not None:
        parent = db.query(Folder).filter(Folder.id == body.parent_id).first()
        if not parent:
            raise HTTPException(status_code=404, detail="Parent folder not found")
        _check_folder_write_access(parent, current_user)

    folder = Folder(
        name=body.name,
        parent_id=body.parent_id,
        owner_id=current_user.id,
    )
    db.add(folder)
    db.commit()
    db.refresh(folder)
    return _to_response(folder, db)


@router.get("", response_model=list[FolderResponse])
def list_folders(db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    """List all folders visible to the current user (flat list)."""
    folders = _visible_folders_query(db, current_user).order_by(Folder.name).all()
    return [_to_response(f, db) for f in folders]


@router.get("/tree", response_model=list[FolderTreeResponse])
def get_folder_tree(db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    """Get the complete folder tree as a nested structure."""
    all_folders = _visible_folders_query(db, current_user).all()

    # Count assets per folder in a single query
    asset_counts = dict(
        db.query(Asset.folder_id, func.count(Asset.id))
        .filter(Asset.folder_id != None)
        .group_by(Asset.folder_id)
        .all()
    )

    # Build a lookup and tree structure
    folder_map = {}
    for f in all_folders:
        folder_map[f.id] = FolderTreeResponse(
            id=f.id,
            name=f.name,
            parent_id=f.parent_id,
            owner_id=f.owner_id,
            is_shared=f.is_shared,
            share_mode=f.share_mode.value if f.share_mode else "read_only",
            asset_count=asset_counts.get(f.id, 0),
            children=[],
        )

    # Assemble the tree — attach children to their parents
    roots = []
    for f in all_folders:
        node = folder_map[f.id]
        if f.parent_id and f.parent_id in folder_map:
            folder_map[f.parent_id].children.append(node)
        else:
            roots.append(node)

    return roots


@router.get("/{folder_id}", response_model=FolderResponse)
def get_folder(folder_id: int, db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    """Get a single folder's details."""
    folder = db.query(Folder).filter(Folder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    _check_folder_read_access(folder, current_user)
    return _to_response(folder, db)


@router.put("/{folder_id}", response_model=FolderResponse)
def update_folder(folder_id: int, body: FolderUpdate, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    """Rename or move a folder. Owner or admin only."""
    folder = db.query(Folder).filter(Folder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    _check_folder_write_access(folder, current_user)

    if body.name is not None:
        folder.name = body.name

    if body.parent_id is not None:
        if body.parent_id == folder_id:
            raise HTTPException(status_code=400, detail="Folder cannot be its own parent")
        # Verify parent exists
        parent = db.query(Folder).filter(Folder.id == body.parent_id).first()
        if not parent:
            raise HTTPException(status_code=404, detail="Parent folder not found")
        # Prevent cycles in the folder tree
        if _would_create_cycle(db, folder_id, body.parent_id):
            raise HTTPException(status_code=400, detail="Moving here would create a circular reference")
        folder.parent_id = body.parent_id

    db.commit()
    db.refresh(folder)
    return _to_response(folder, db)


@router.put("/{folder_id}/share", response_model=FolderResponse)
def update_folder_sharing(folder_id: int, body: FolderShareUpdate,
                          db: Session = Depends(get_db),
                          current_user: User = Depends(get_current_user)):
    """Set folder sharing options. Admin only."""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only admins can set folder sharing")

    folder = db.query(Folder).filter(Folder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")

    folder.is_shared = body.is_shared
    try:
        folder.share_mode = FolderShareMode(body.share_mode)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid share mode. Use 'read_only' or 'read_write'")

    db.commit()
    db.refresh(folder)
    return _to_response(folder, db)


@router.delete("/{folder_id}", status_code=204)
def delete_folder(folder_id: int, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    """Delete a folder. Assets in this folder become unfiled (folder_id = NULL).
    Child folders are cascade-deleted by the database."""
    folder = db.query(Folder).filter(Folder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    _check_folder_write_access(folder, current_user)

    # Unfile assets before deleting the folder (SET NULL happens via FK but let's be explicit)
    db.query(Asset).filter(Asset.folder_id == folder_id).update({Asset.folder_id: None})
    db.delete(folder)
    db.commit()


@router.post("/move-assets", status_code=200)
def move_assets(body: MoveAssetsRequest, db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    """Move assets into a folder, or set folder_id=null to unfile them."""
    # If moving to a folder, verify it exists and user has write access
    if body.folder_id is not None:
        folder = db.query(Folder).filter(Folder.id == body.folder_id).first()
        if not folder:
            raise HTTPException(status_code=404, detail="Target folder not found")
        _check_folder_write_access(folder, current_user)

    # Verify the user has access to all the assets being moved
    assets = db.query(Asset).filter(Asset.id.in_(body.asset_ids)).all()
    if len(assets) != len(body.asset_ids):
        raise HTTPException(status_code=404, detail="One or more assets not found")

    for asset in assets:
        if not current_user.is_admin and asset.owner_id != current_user.id:
            # Check if asset is in a read-write shared folder
            if asset.folder_id:
                source_folder = db.query(Folder).filter(Folder.id == asset.folder_id).first()
                if not (source_folder and source_folder.is_shared
                        and source_folder.share_mode == FolderShareMode.READ_WRITE):
                    raise HTTPException(status_code=403, detail="Access denied to one or more assets")
            else:
                raise HTTPException(status_code=403, detail="Access denied to one or more assets")

    # Perform the move
    db.query(Asset).filter(Asset.id.in_(body.asset_ids)).update(
        {Asset.folder_id: body.folder_id}, synchronize_session="fetch"
    )
    db.commit()
    return {"moved": len(body.asset_ids), "folder_id": body.folder_id}
