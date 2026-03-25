"""
Presentation management routes — upload, convert, navigate, and serve slides.

Provides:
- POST /api/presentations/upload          — Upload a slideshow file (starts background conversion)
- GET  /api/presentations/                — List all presentations (admin: all, user: own only)
- GET  /api/presentations/{id}            — Get single presentation details
- DELETE /api/presentations/{id}          — Delete presentation and slide files from disk
- POST /api/presentations/{id}/navigate   — Set current slide number (authenticated)
- GET  /api/presentations/{id}/current    — Get current slide number (no auth — polled by viewer)
- GET  /api/presentations/{id}/slide/{num} — Serve a single slide PNG image (no auth)
- GET  /api/presentations/{id}/viewer     — Serve the slide viewer HTML page (no auth)

Auth-free endpoints (current, slide, viewer) are unauthenticated because they're
loaded inside the browser source container's Firefox instance, which has no JWT.
These serve non-sensitive slide content and are safe to leave open.

RBAC: Admins see all presentations. Regular users see only their own uploads.
"""

import asyncio
import logging
import os
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session

from backend.database import get_db, SessionLocal
from backend.models import Presentation, PresentationStatus, User
from backend.schemas import (
    PresentationResponse, PresentationListResponse, SlideNavigateRequest,
)
from backend.auth import get_current_user
from backend.config import UPLOAD_DIR, PRESENTATIONS_DIR
from backend.services.presentation_converter import (
    convert_presentation, PRESENTATION_EXTENSIONS,
)

logger = logging.getLogger("presentations")
router = APIRouter(prefix="/api/presentations", tags=["presentations"])


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
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upload a slideshow file and kick off background conversion to slide PNGs.

    Accepts PPTX, PPT, ODP, and PDF files. The file is saved to the uploads
    directory, a Presentation record is created, and a background task runs
    LibreOffice headless to convert each slide to a PNG image.

    Returns the newly created presentation in UPLOADING status.
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
    upload_path = UPLOAD_DIR / unique_name

    # Stream upload to disk in 1MB chunks
    try:
        with open(upload_path, "wb") as dest:
            while chunk := await file.read(1024 * 1024):
                dest.write(chunk)
    except OSError as exc:
        if upload_path.exists():
            os.remove(upload_path)
        logger.error("Presentation upload failed for %s: %s", user_filename, exc)
        raise HTTPException(status_code=500, detail=f"Upload failed: {exc}")

    # Strip the file extension for a cleaner display name
    display_name = Path(user_filename).stem

    presentation = Presentation(
        name=display_name,
        owner_id=current_user.id,
        status=PresentationStatus.UPLOADING,
    )
    db.add(presentation)
    db.commit()
    db.refresh(presentation)

    # Conversion runs in background with its own DB session
    background_tasks.add_task(
        convert_presentation, presentation.id, str(upload_path), SessionLocal,
    )
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
    """Delete a presentation and its slide files from disk.

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

    # Remove slide files from disk
    if presentation.slides_dir and os.path.isdir(presentation.slides_dir):
        try:
            shutil.rmtree(presentation.slides_dir)
            logger.info("Deleted slide directory: %s", presentation.slides_dir)
        except OSError as exc:
            logger.warning("Failed to delete slide directory %s: %s",
                           presentation.slides_dir, exc)

    db.delete(presentation)
    db.commit()
    return None


# ---------------------------------------------------------------------------
# Slide navigation (authenticated — called by the control panel UI)
# ---------------------------------------------------------------------------

@router.post("/{presentation_id}/navigate", response_model=PresentationResponse)
def navigate_slide(
    presentation_id: int,
    body: SlideNavigateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Set the current slide for a presentation.

    The slide viewer HTML page polls the /current endpoint and updates
    when it detects a change. This endpoint is called by the frontend
    control panel when the operator clicks prev/next/go-to.

    The slide number is 1-indexed and clamped to the valid range.
    """
    presentation = db.query(Presentation).filter(
        Presentation.id == presentation_id
    ).first()
    if not presentation:
        raise HTTPException(status_code=404, detail="Presentation not found")
    if presentation.status != PresentationStatus.READY:
        raise HTTPException(status_code=400, detail="Presentation is not ready")

    # Clamp slide number to valid range
    target_slide = max(1, min(body.slide, presentation.slide_count))
    presentation.current_slide = target_slide
    db.commit()
    db.refresh(presentation)

    return _to_response(presentation)


# ---------------------------------------------------------------------------
# Unauthenticated endpoints — used by the slide viewer inside the container
# ---------------------------------------------------------------------------

@router.get("/{presentation_id}/current")
def get_current_slide(
    presentation_id: int,
    db: Session = Depends(get_db),
):
    """Get the current slide number. No auth — polled by the viewer page.

    Returns a minimal JSON payload that the viewer JavaScript fetches
    every second to detect slide changes.
    """
    presentation = db.query(Presentation).filter(
        Presentation.id == presentation_id
    ).first()
    if not presentation:
        raise HTTPException(status_code=404, detail="Presentation not found")
    return {
        "current_slide": presentation.current_slide or 1,
        "slide_count": presentation.slide_count or 0,
    }


@router.get("/{presentation_id}/slide/{slide_num}")
def get_slide_image(
    presentation_id: int,
    slide_num: int,
    db: Session = Depends(get_db),
):
    """Serve a single slide PNG image. No auth — loaded by img tags in the viewer.

    The slide_num is 1-indexed and maps to slide_001.png, slide_002.png, etc.
    """
    presentation = db.query(Presentation).filter(
        Presentation.id == presentation_id
    ).first()
    if not presentation:
        raise HTTPException(status_code=404, detail="Presentation not found")
    if not presentation.slides_dir:
        raise HTTPException(status_code=404, detail="Slides not available")

    slide_path = Path(presentation.slides_dir) / f"slide_{slide_num:03d}.png"
    if not slide_path.exists():
        raise HTTPException(status_code=404, detail=f"Slide {slide_num} not found")

    return FileResponse(str(slide_path), media_type="image/png")


@router.get("/{presentation_id}/viewer", response_class=HTMLResponse)
def slide_viewer(
    presentation_id: int,
    db: Session = Depends(get_db),
):
    """Serve the slide viewer HTML page. No auth — loaded by Firefox in the container.

    This is a self-contained HTML page with inline CSS and JavaScript that:
    - Displays the current slide as a full-screen image
    - Polls /api/presentations/{id}/current every second
    - Crossfades to the new slide when the current_slide value changes
    - Uses a dark background to match broadcast aesthetics

    The browser source container loads this URL in Firefox kiosk mode,
    and ffmpeg captures the display via x11grab for multicast output.
    """
    presentation = db.query(Presentation).filter(
        Presentation.id == presentation_id
    ).first()
    if not presentation:
        raise HTTPException(status_code=404, detail="Presentation not found")

    # Self-contained HTML page — no external dependencies needed inside the container
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{presentation.name} — Slide Viewer</title>
<style>
    /* Full-screen dark canvas — matches broadcast output expectations */
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    html, body {{
        width: 100vw;
        height: 100vh;
        overflow: hidden;
        background: #000;
    }}
    /* Slide container holds both current and incoming slide for crossfade */
    .slide-container {{
        position: relative;
        width: 100vw;
        height: 100vh;
    }}
    .slide-container img {{
        position: absolute;
        top: 0;
        left: 0;
        width: 100vw;
        height: 100vh;
        object-fit: contain;
        transition: opacity 0.4s ease-in-out;
    }}
    /* The incoming slide starts transparent and fades in */
    .slide-container img.incoming {{
        opacity: 0;
    }}
    .slide-container img.active {{
        opacity: 1;
    }}
</style>
</head>
<body>
<div class="slide-container">
    <img id="slideA" class="active" src="/api/presentations/{presentation.id}/slide/1" alt="Slide">
    <img id="slideB" class="incoming" src="" alt="Slide">
</div>
<script>
    // Slide viewer polling script
    // Alternates between two img elements for smooth crossfade transitions
    const PRESENTATION_ID = {presentation.id};
    const POLL_INTERVAL_MS = 1000;

    let currentSlide = 1;
    let activeImg = document.getElementById('slideA');
    let incomingImg = document.getElementById('slideB');

    async function pollCurrentSlide() {{
        try {{
            const resp = await fetch(`/api/presentations/${{PRESENTATION_ID}}/current`);
            if (!resp.ok) return;
            const data = await resp.json();
            const newSlide = data.current_slide;

            if (newSlide !== currentSlide) {{
                currentSlide = newSlide;
                // Load the new slide into the hidden (incoming) image element
                incomingImg.src = `/api/presentations/${{PRESENTATION_ID}}/slide/${{newSlide}}`;
                incomingImg.onload = () => {{
                    // Crossfade: show incoming, hide active
                    incomingImg.classList.remove('incoming');
                    incomingImg.classList.add('active');
                    activeImg.classList.remove('active');
                    activeImg.classList.add('incoming');
                    // Swap references so the next transition uses the other element
                    [activeImg, incomingImg] = [incomingImg, activeImg];
                }};
            }}
        }} catch (err) {{
            // Silently retry on network errors — container may still be starting
        }}
    }}

    // Start polling immediately
    setInterval(pollCurrentSlide, POLL_INTERVAL_MS);
</script>
</body>
</html>"""
    return HTMLResponse(content=html)
