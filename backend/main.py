"""
Multicast Streamer — FastAPI entry point.

This is the top-level module that bootstraps the entire application:
  - Registers all API route modules (auth, assets, streams, settings)
  - Runs Alembic migrations on startup (PostgreSQL schema management)
  - Seeds a default admin user and server settings
  - Initializes the StreamManager (ffmpeg playlist playout) and
    WaylandManager (native Wayland capture pipeline) singletons
  - Serves the React SPA from the frontend build directory
  - Gracefully shuts down all active streams on exit

The FastAPI lifespan context manager handles startup/shutdown orchestration.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from alembic.config import Config as AlembicConfig
from alembic import command as alembic_command
from backend.config import CORS_ORIGINS, DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD
from backend.database import SessionLocal
from backend.models import User, Asset, AssetStatus, Presentation, PresentationStatus
from backend.auth import hash_password
from backend.services.stream_manager import StreamManager
from backend.services.wayland_manager import WaylandManager
from backend.routes import auth as auth_routes
from backend.routes import assets as asset_routes
from backend.routes import streams as stream_routes
from backend.routes import settings as settings_routes
from backend.routes import folders as folder_routes
from backend.routes import presentations as presentation_routes

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("main")


def _run_alembic_migrations():
    """
    Run Alembic migrations to bring the database schema up to date.

    Uses the alembic.ini config relative to the project root. This is called
    once during application startup so the schema is always current without
    requiring a separate migration step during deployment.
    """
    project_root = Path(__file__).parent.parent
    alembic_cfg = AlembicConfig(str(project_root / "alembic.ini"))
    # Override the script_location to use an absolute path so it works
    # regardless of the working directory
    alembic_cfg.set_main_option("script_location", str(project_root / "alembic"))
    alembic_command.upgrade(alembic_cfg, "head")
    logger.info("Alembic migrations applied")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan context manager — runs once at startup and shutdown.

    Startup sequence:
      1. Run Alembic migrations (creates/updates PostgreSQL schema)
      2. Seed default admin user (if not present)
      3. Seed default server settings
      4. Apply persisted settings to runtime config module
      5. Reset stale PROCESSING assets from prior crashes
      6. Initialize StreamManager and BrowserManager
      7. Restore streams that were running before restart

    Shutdown sequence:
      1. Kill all active playlist stream ffmpeg processes
      2. Kill all browser source containers

    Args:
        app: The FastAPI application instance. Managers are stored on app.state
             so route handlers can access them via request.app.state.
    """
    # --- Startup ---
    logger.info("Running database migrations...")
    _run_alembic_migrations()

    db = SessionLocal()
    try:
        # Create default admin user if the database is fresh or the user was deleted
        if not db.query(User).filter(User.username == DEFAULT_ADMIN_USERNAME).first():
            db.add(User(username=DEFAULT_ADMIN_USERNAME,
                        hashed_password=hash_password(DEFAULT_ADMIN_PASSWORD),
                        is_active=True, is_admin=True))
            db.commit()
            logger.info("Created default admin user: %s", DEFAULT_ADMIN_USERNAME)

        # Seed default server settings (idempotent — skips keys that already exist)
        settings_routes.seed_default_settings(db)

        # Push persisted DB settings into the runtime config module so they
        # take effect immediately (e.g., transcode resolution, multicast TTL)
        settings_routes._apply_runtime_settings(db)
    finally:
        db.close()

    # Reset any assets stuck in PROCESSING state from a previous crash.
    # If the server was killed mid-transcode, these assets will never complete
    # on their own — mark them as ERROR so the user knows to re-upload.
    db = SessionLocal()
    try:
        stale_count = db.query(Asset).filter(
            Asset.status == AssetStatus.PROCESSING
        ).update({Asset.status: AssetStatus.ERROR,
                  Asset.error_message: "Server restarted during transcode"})
        if stale_count:
            db.commit()
            logger.info("Reset %d stale PROCESSING assets to ERROR", stale_count)

        # Same treatment for presentations stuck mid-conversion
        stale_pres = db.query(Presentation).filter(
            Presentation.status == PresentationStatus.PROCESSING
        ).update({Presentation.status: PresentationStatus.ERROR,
                  Presentation.error_message: "Server restarted during conversion"})
        if stale_pres:
            db.commit()
            logger.info("Reset %d stale PROCESSING presentations to ERROR", stale_pres)
    finally:
        db.close()

    # StreamManager handles ffmpeg concat-demuxer subprocesses for playlist streams.
    # WaylandManager handles native Wayland capture pipelines for browser/presentation sources.
    # Both need a session factory to update stream status in the DB independently.
    app.state.stream_manager = StreamManager(db_session_factory=SessionLocal)
    # Attribute name kept as browser_manager for API compatibility with routes/streams.py
    app.state.browser_manager = WaylandManager(db_session_factory=SessionLocal)

    # Restore capture sources that were marked as running before a server restart.
    # This re-launches their Wayland process groups with the same display/port config.
    try:
        await app.state.browser_manager.restore_sessions()
    except Exception as exc:
        logger.error("Browser session restore failed: %s", exc)

    # Restore playlist streams that were running before the server restarted.
    # Unlike browser sources (which need container re-creation), playlist streams
    # just need their ffmpeg process relaunched with the same concat file.
    try:
        await app.state.stream_manager.restore_sessions()
    except Exception as exc:
        logger.error("Playlist stream restore failed: %s", exc)

    logger.info("Multicast Streamer ready")
    yield  # Application is running — control returns here on shutdown

    # --- Shutdown ---
    logger.info("Stopping all streams and capture sources...")
    await app.state.stream_manager.stop_all()
    await app.state.browser_manager.stop_all()


# ── FastAPI application instance ──────────────────────────────────────────────
app = FastAPI(title="Multicast Streamer",
              description="Media library + MPEG-TS multicast playout",
              version="2.0.0", lifespan=lifespan)

# Allow cross-origin requests from the React dev server (localhost:3000)
# and any other origins specified via MCS_CORS_ORIGINS env var.
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS,
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# Register API route modules — all prefixed with /api/ (defined in each router)
app.include_router(auth_routes.router)
app.include_router(asset_routes.router)
app.include_router(stream_routes.router)
app.include_router(settings_routes.router)
app.include_router(folder_routes.router)
app.include_router(presentation_routes.router)

# ── React SPA static file serving ────────────────────────────────────────────
# In production, the React app is pre-built into frontend/dist/.
# We serve /assets/* directly and route everything else to index.html
# so that React Router handles client-side navigation.
FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"
if FRONTEND_DIST.exists():
    # Vite outputs hashed assets to dist/assets/ (CRA used dist/static/)
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """Catch-all route that serves the React SPA's index.html."""
        # Don't intercept API routes — let FastAPI's router handle those
        if full_path.startswith("api/"):
            return None
        index = FRONTEND_DIST / "index.html"
        return FileResponse(str(index)) if index.exists() else {"detail": "Frontend not built"}
else:
    # Development mode — frontend is served by Vite dev server on :3000
    @app.get("/")
    async def root():
        """Minimal response when frontend build is not present."""
        return {"message": "API running", "docs": "/docs", "note": "Frontend not built"}
