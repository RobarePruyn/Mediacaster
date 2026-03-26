"""
Presentation converter — converts uploaded slideshow files to per-slide PNG images
using LibreOffice headless + pdftoppm (poppler-utils).

Pipeline: PPTX/PPT/ODP → LibreOffice → PDF → pdftoppm → per-page PNGs.
PDF uploads skip the LibreOffice step and go straight to pdftoppm.

This two-step approach is necessary because LibreOffice's --convert-to png
only produces a single image for presentation files — it doesn't split slides.
pdftoppm reliably splits each PDF page into an individual PNG.

Supports PPTX, PPT, ODP, and PDF formats. Each presentation gets its own subdirectory
under PRESENTATIONS_DIR containing sequentially named slide images (slide_001.png, etc.).

Follows the same background-task pattern as the transcoder service: upload handler kicks
off an async task, which acquires a semaphore slot, runs the conversion, and updates
the DB with results or errors.
"""

import asyncio
import logging
import os
import shutil
from pathlib import Path
from sqlalchemy.orm import Session
from backend import config
from backend.models import Presentation, PresentationStatus

logger = logging.getLogger("presentation_converter")

# Limit concurrent conversions — LibreOffice is memory-heavy
_convert_semaphore = asyncio.Semaphore(1)

# File extensions accepted for presentation upload
PRESENTATION_EXTENSIONS = {".pptx", ".ppt", ".odp", ".pdf"}


async def convert_presentation(presentation_id: int, upload_path: str,
                                db_session_factory) -> None:
    """
    Convert an uploaded presentation file to per-slide PNG images.

    Runs LibreOffice in headless mode to export each slide as a PNG. The output
    files are renamed to a predictable slide_NNN.png pattern for easy serving.

    Args:
        presentation_id: Database ID of the Presentation record
        upload_path: Path to the uploaded file (will be deleted after conversion)
        db_session_factory: Callable returning a new SQLAlchemy Session
    """
    async with _convert_semaphore:
        db: Session = db_session_factory()
        try:
            presentation = db.query(Presentation).filter(
                Presentation.id == presentation_id
            ).first()
            if not presentation:
                logger.error("Presentation %d not found", presentation_id)
                return

            presentation.status = PresentationStatus.PROCESSING
            db.commit()

            # Create output directory for this presentation's slides
            slides_dir = str(config.PRESENTATIONS_DIR / str(presentation_id))
            os.makedirs(slides_dir, exist_ok=True)

            upload_ext = Path(upload_path).suffix.lower()

            # Step 1: Get a PDF.
            # If the upload is already a PDF, use it directly.
            # Otherwise, convert PPTX/PPT/ODP → PDF via LibreOffice headless.
            if upload_ext == ".pdf":
                pdf_path = upload_path
            else:
                cmd_to_pdf = [
                    config.LIBREOFFICE_PATH,
                    "--headless",
                    "--convert-to", "pdf",
                    "--outdir", slides_dir,
                    upload_path,
                ]
                logger.info("Converting presentation %d to PDF: %s",
                            presentation_id, " ".join(cmd_to_pdf))

                proc = await asyncio.create_subprocess_exec(
                    *cmd_to_pdf,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()

                if proc.returncode != 0:
                    error_msg = stderr.decode().strip() or stdout.decode().strip()
                    logger.error("LibreOffice PDF conversion failed for presentation %d: %s",
                                 presentation_id, error_msg)
                    presentation.status = PresentationStatus.ERROR
                    presentation.error_message = f"PDF conversion failed: {error_msg[:500]}"
                    db.commit()
                    return

                # LibreOffice names the output based on the input filename
                pdf_files = list(Path(slides_dir).glob("*.pdf"))
                if not pdf_files:
                    presentation.status = PresentationStatus.ERROR
                    presentation.error_message = "LibreOffice produced no PDF output"
                    db.commit()
                    return
                pdf_path = str(pdf_files[0])

            # Step 2: Split PDF pages into individual PNGs using pdftoppm.
            # pdftoppm outputs files named <prefix>-01.png, <prefix>-02.png, etc.
            # -r 150 gives 1920x1080-ish output for standard 16:9 slides at good quality.
            png_prefix = os.path.join(slides_dir, "slide")
            cmd_pdftoppm = [
                "pdftoppm",
                "-png",           # Output format
                "-r", "150",      # Resolution in DPI (150 ≈ 1920px wide for 16:9)
                pdf_path,
                png_prefix,
            ]
            logger.info("Splitting PDF to PNGs for presentation %d: %s",
                        presentation_id, " ".join(cmd_pdftoppm))

            proc2 = await asyncio.create_subprocess_exec(
                *cmd_pdftoppm,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout2, stderr2 = await proc2.communicate()

            if proc2.returncode != 0:
                error_msg = stderr2.decode().strip() or stdout2.decode().strip()
                logger.error("pdftoppm failed for presentation %d: %s",
                             presentation_id, error_msg)
                presentation.status = PresentationStatus.ERROR
                presentation.error_message = f"PDF split failed: {error_msg[:500]}"
                db.commit()
                return

            # Clean up the intermediate PDF (unless the upload was already a PDF)
            if upload_ext != ".pdf" and os.path.exists(pdf_path):
                try:
                    os.remove(pdf_path)
                except OSError:
                    pass

            # Collect all PNGs and rename to slide_001.png, slide_002.png, etc.
            # pdftoppm names them slide-01.png or slide-1.png depending on page count
            png_files = sorted(Path(slides_dir).glob("slide-*.png"))

            if not png_files:
                presentation.status = PresentationStatus.ERROR
                presentation.error_message = "No slides produced — unsupported format or empty file"
                db.commit()
                return

            slide_count = 0
            for i, png_file in enumerate(png_files, start=1):
                new_name = Path(slides_dir) / f"slide_{i:03d}.png"
                if png_file != new_name:
                    png_file.rename(new_name)
                slide_count = i

            # Clean up the original upload file
            try:
                os.remove(upload_path)
            except OSError:
                pass

            # Update the presentation record with results
            presentation.slide_count = slide_count
            presentation.slides_dir = slides_dir
            presentation.current_slide = 1
            presentation.status = PresentationStatus.READY
            presentation.error_message = None
            db.commit()

            logger.info("Presentation %d converted: %d slides in %s",
                        presentation_id, slide_count, slides_dir)

        except Exception as exc:
            logger.exception("Unexpected error converting presentation %d", presentation_id)
            try:
                presentation = db.query(Presentation).filter(
                    Presentation.id == presentation_id
                ).first()
                if presentation:
                    presentation.status = PresentationStatus.ERROR
                    presentation.error_message = str(exc)[:500]
                    db.commit()
            except Exception:
                pass
        finally:
            db.close()
