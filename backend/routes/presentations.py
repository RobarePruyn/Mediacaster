"""
Presentation management routes — upload, list, and delete slideshow files.

Provides:
- POST /api/presentations/upload          — Upload a slideshow file (stored for container use)
- GET  /api/presentations/                — List all presentations (admin: all, user: own only)
- GET  /api/presentations/{id}            — Get single presentation details
- DELETE /api/presentations/{id}          — Delete presentation and file from disk

Presentations are stored as-is (PPTX, ODP, PDF) and mounted into the capture
container at stream start time. LibreOffice Impress renders them natively in
slideshow mode, preserving animations, transitions, and embedded media.

Slide navigation is handled via xdotool key events sent through the stream's
slide-control endpoint (POST /api/streams/{id}/slide-control), not through
this router.

RBAC: Admins see all presentations. Regular users see only their own uploads.
"""

import logging
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import Presentation, PresentationStatus, User
from backend.schemas import (
    PresentationResponse, PresentationListResponse,
)
from backend.auth import get_current_user
from backend.config import PRESENTATIONS_DIR

logger = logging.getLogger("presentations")
router = APIRouter(prefix="/api/presentations", tags=["presentations"])

# File extensions accepted for presentation upload
PRESENTATION_EXTENSIONS = {".pptx", ".ppt", ".odp", ".pdf"}


def _to_response(presentation: Presentation) -> PresentationResponse:
    """Convert a Presentation ORM object to the API response schema."""
    return PresentationResponse(
        id=presentation.id,
        name=presentation.name,
        owner_id=presentation.owner_id,
        slide_count=presentation.slide_count or 0,
        current_slide=presentation.current_slide or 1,
        status=presentation.status.value if presentation.status else "unknown",
        error_message=presentation.error_message,
        created_at=presentation.created_at,
        updated_at=presentation.updated_at,
    )


# ---------------------------------------------------------------------------
# Upload and CRUD
# ---------------------------------------------------------------------------

@router.post("/upload", response_model=PresentationResponse, status_code=201)
async def upload_presentation(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upload a slideshow file for use with presentation source streams.

    Accepts PPTX, PPT, ODP, and PDF files. The file is saved to the
    presentations directory and a Presentation record is created with
    READY status — no conversion is needed because LibreOffice Impress
    renders the file natively inside the capture container.
    """
    user_filename = file.filename or "unknown"
    ext = Path(user_filename).suffix.lower()
    if ext not in PRESENTATION_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Accepted: {', '.join(sorted(PRESENTATION_EXTENSIONS))}",
        )

    # UUID filename on disk to prevent collisions and path traversal
    unique_name = f"{uuid.uuid4().hex}{ext}"
    file_path = PRESENTATIONS_DIR / unique_name

    # Stream upload to disk in 1MB chunks
    try:
        with open(file_path, "wb") as dest:
            while chunk := await file.read(1024 * 1024):
                dest.write(chunk)
    except OSError as exc:
        if file_path.exists():
            os.remove(file_path)
        logger.error("Presentation upload failed for %s: %s", user_filename, exc)
        raise HTTPException(status_code=500, detail=f"Upload failed: {exc}")

    # Strip the file extension for a cleaner display name
    display_name = Path(user_filename).stem

    # No background conversion needed — LibreOffice renders natively in the container.
    # Mark as READY immediately so it can be selected for a stream.
    presentation = Presentation(
        name=display_name,
        owner_id=current_user.id,
        file_path=str(file_path),
        status=PresentationStatus.READY,
    )
    db.add(presentation)
    db.commit()
    db.refresh(presentation)

    logger.info("Presentation uploaded: id=%d name='%s' path=%s",
                presentation.id, display_name, file_path)
    return _to_response(presentation)


@router.get("/", response_model=PresentationListResponse)
def list_presentations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List presentations. Admins see all; regular users see only their own."""
    query = db.query(Presentation)
    if not current_user.is_admin:
        query = query.filter(Presentation.owner_id == current_user.id)
    # Most recent first
    presentations = query.order_by(Presentation.created_at.desc()).all()
    return PresentationListResponse(
        presentations=[_to_response(p) for p in presentations],
        total_count=len(presentations),
    )


@router.get("/{presentation_id}", response_model=PresentationResponse)
def get_presentation(
    presentation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a single presentation by ID. Enforces ownership for non-admins."""
    presentation = db.query(Presentation).filter(
        Presentation.id == presentation_id
    ).first()
    if not presentation:
        raise HTTPException(status_code=404, detail="Presentation not found")
    if not current_user.is_admin and presentation.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    return _to_response(presentation)


@router.delete("/{presentation_id}", status_code=204)
def delete_presentation(
    presentation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a presentation and its file from disk.

    Admins can delete any presentation. Regular users can only delete their own.
    Any browser sources linked to this presentation will have their
    presentation_id set to NULL (via the FK ondelete=SET NULL).
    """
    presentation = db.query(Presentation).filter(
        Presentation.id == presentation_id
    ).first()
    if not presentation:
        raise HTTPException(status_code=404, detail="Presentation not found")
    if not current_user.is_admin and presentation.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Remove the presentation file from disk
    if presentation.file_path and os.path.isfile(presentation.file_path):
        try:
            os.remove(presentation.file_path)
            logger.info("Deleted presentation file: %s", presentation.file_path)
        except OSError as exc:
            logger.warning("Failed to delete presentation file %s: %s",
                           presentation.file_path, exc)

    # Clean up legacy slide directory if it exists
    if presentation.slides_dir and os.path.isdir(presentation.slides_dir):
        import shutil
        try:
            shutil.rmtree(presentation.slides_dir)
            logger.info("Deleted legacy slide directory: %s", presentation.slides_dir)
        except OSError as exc:
            logger.warning("Failed to delete slide directory %s: %s",
                           presentation.slides_dir, exc)

    db.delete(presentation)
    db.commit()
    return None
