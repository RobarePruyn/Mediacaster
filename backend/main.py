"""
Multicast Streamer — FastAPI entry point.

This is the top-level module that bootstraps the entire application:
  - Registers all API route modules (auth, assets, streams, settings)
  - Runs SQLite schema migrations on startup
  - Seeds a default admin user and server settings
  - Initializes the StreamManager (ffmpeg playlist playout) and
    BrowserManager (Podman-based browser capture) singletons
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
from sqlalchemy import text
from backend.config import CORS_ORIGINS, DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD
from backend.database import init_db, SessionLocal
from backend.models import User, Asset, AssetStatus, Stream, StreamStatus, StreamSourceType
from backend.auth import hash_password
from backend.services.stream_manager import StreamManager
from backend.services.browser_manager import BrowserManager
from backend.routes import auth as auth_routes
from backend.routes import assets as asset_routes
from backend.routes import streams as stream_routes
from backend.routes import settings as settings_routes

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("main")


def _run_migrations(db):
    """
    Lightweight SQLite schema migrations for upgrades.

    SQLite doesn't support full ALTER TABLE, so we use a simple pattern:
    try to SELECT the column — if it fails, ADD it. This avoids needing
    a dedicated migration framework (Alembic) for this small schema.

    Args:
        db: An active SQLAlchemy Session used to execute raw SQL statements.
    """
    # Each tuple: (table_name, column_name, column_definition)
    # These represent columns added after the initial schema was deployed.
    migrations = [
        ("users", "must_change_password", "BOOLEAN DEFAULT 1"),
        ("assets", "display_name", "VARCHAR(512)"),
        ("assets", "transcode_progress", "FLOAT DEFAULT 0.0"),
        ("assets", "source_duration_seconds", "FLOAT"),
        ("assets", "owner_id", "INTEGER"),
        ("streams", "source_type", "VARCHAR(32) DEFAULT 'playlist'"),
    ]
    for table, column, col_type in migrations:
        try:
            # Probe whether the column exists by selecting from it
            db.execute(text(f"SELECT {column} FROM {table} LIMIT 1"))
        except Exception:
            # Column doesn't exist yet — add it to the table
            logger.info("Migration: adding %s.%s", table, column)
            db.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
            db.commit()

    # Backfill display_name for assets created before that column existed.
    # display_name defaults to the original uploaded filename.
    try:
        db.execute(text(
            "UPDATE assets SET display_name = original_filename "
            "WHERE display_name IS NULL"
        ))
        db.commit()
    except Exception as exc:
        # Non-critical: display_name is cosmetic. Log and continue.
        logger.warning("display_name backfill skipped: %s", exc)

    # Assign unowned assets to the first admin user so they remain
    # accessible in the UI after the owner_id column was introduced.
    try:
        db.execute(text(
            "UPDATE assets SET owner_id = ("
            "  SELECT id FROM users WHERE is_admin = 1 LIMIT 1"
            ") WHERE owner_id IS NULL"
        ))
        db.commit()
    except Exception as exc:
        # Non-critical: assets will just appear unowned. Log and continue.
        logger.warning("owner_id backfill skipped: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan context manager — runs once at startup and shutdown.

    Startup sequence:
      1. Create database tables (if missing)
      2. Run column-level migrations
      3. Seed default admin user (if not present)
      4. Seed default server settings
      5. Apply persisted settings to runtime config module
      6. Initialize StreamManager and BrowserManager
      7. Restore any browser sources that were running before restart

    Shutdown sequence:
      1. Stop all active playlist streams (kills ffmpeg processes)
      2. Stop all browser source containers (podman rm)

    Args:
        app: The FastAPI application instance. Managers are stored on app.state
             so route handlers can access them via request.app.state.
    """
    # --- Startup ---
    logger.info("Initializing database...")
    init_db()

    db = SessionLocal()
    try:
        _run_migrations(db)

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
    finally:
        db.close()

    # StreamManager handles ffmpeg concat-demuxer subprocesses for playlist streams.
    # BrowserManager handles Podman containers for browser source capture.
    # Both need a session factory to update stream status in the DB independently.
    app.state.stream_manager = StreamManager(db_session_factory=SessionLocal)
    app.state.browser_manager = BrowserManager(db_session_factory=SessionLocal)

    # Restore browser sources that were marked as running before a server restart.
    # This re-launches their Podman containers with the same display/port config.
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
    logger.info("Stopping all streams and browser sources...")
    await app.state.stream_manager.stop_all()
    await app.state.browser_manager.stop_all()


# ── FastAPI application instance ──────────────────────────────────────────────
app = FastAPI(title="Multicast Streamer",
              description="Media library + MPEG-TS multicast playout",
              version="1.1.0", lifespan=lifespan)

# Allow cross-origin requests from the React dev server (localhost:3000)
# and any other origins specified via MCS_CORS_ORIGINS env var.
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS,
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# Register API route modules — all prefixed with /api/ (defined in each router)
app.include_router(auth_routes.router)
app.include_router(asset_routes.router)
app.include_router(stream_routes.router)
app.include_router(settings_routes.router)

# ── React SPA static file serving ────────────────────────────────────────────
# In production, the React app is pre-built into frontend/dist/.
# We serve /static/* directly and route everything else to index.html
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
