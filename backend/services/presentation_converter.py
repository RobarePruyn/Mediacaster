"""
Presentation converter — converts uploaded slideshow files to per-slide PNG images
using LibreOffice headless.

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

            # Run LibreOffice headless to convert slides to PNG
            cmd = [
                config.LIBREOFFICE_PATH,
                "--headless",
                "--convert-to", "png",
                "--outdir", slides_dir,
                upload_path,
            ]
            logger.info("Converting presentation %d: %s", presentation_id, " ".join(cmd))

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                error_msg = stderr.decode().strip() or stdout.decode().strip()
                logger.error("LibreOffice conversion failed for presentation %d: %s",
                             presentation_id, error_msg)
                presentation.status = PresentationStatus.ERROR
                presentation.error_message = f"Conversion failed: {error_msg[:500]}"
                db.commit()
                return

            # For single-file inputs (PPTX), LibreOffice produces one PNG.
            # For multi-page PDFs, it produces multiple PNGs.
            # For PPTX/ODP, LibreOffice actually produces one PNG per slide
            # but only when using the "Impress" filter. The default --convert-to png
            # for presentations may produce one file per slide or a single file.
            #
            # We need to handle both cases. LibreOffice names output files based on
            # the input filename, e.g., "presentation.png" for single or
            # "presentation-1.png", "presentation-2.png" for multiple.
            #
            # Collect all PNG files and rename to slide_001.png, slide_002.png, etc.
            png_files = sorted(Path(slides_dir).glob("*.png"))

            if not png_files:
                # Fallback: try PDF-based conversion for PPTX
                # First convert to PDF, then PDF pages to PNGs
                pdf_path = os.path.join(slides_dir, "temp_convert.pdf")
                cmd_to_pdf = [
                    config.LIBREOFFICE_PATH,
                    "--headless",
                    "--convert-to", "pdf",
                    "--outdir", slides_dir,
                    upload_path,
                ]
                proc2 = await asyncio.create_subprocess_exec(
                    *cmd_to_pdf,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc2.communicate()

                # Find the produced PDF
                pdf_files = list(Path(slides_dir).glob("*.pdf"))
                if pdf_files:
                    pdf_path = str(pdf_files[0])
                    # Use ffmpeg to extract PDF pages as PNGs
                    # (LibreOffice may not split PDF pages to individual PNGs)
                    # Actually, let's use pdftoppm if available, or try
                    # LibreOffice again with the PDF as input
                    cmd_pdf_to_png = [
                        config.LIBREOFFICE_PATH,
                        "--headless",
                        "--convert-to", "png",
                        "--outdir", slides_dir,
                        pdf_path,
                    ]
                    proc3 = await asyncio.create_subprocess_exec(
                        *cmd_pdf_to_png,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await proc3.communicate()
                    # Clean up temp PDF
                    try:
                        os.remove(pdf_path)
                    except OSError:
                        pass

                png_files = sorted(Path(slides_dir).glob("*.png"))

            if not png_files:
                presentation.status = PresentationStatus.ERROR
                presentation.error_message = "No slides produced — unsupported format or empty file"
                db.commit()
                return

            # Rename all PNGs to a predictable sequential pattern
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
