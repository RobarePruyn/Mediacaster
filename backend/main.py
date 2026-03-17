"""
Multicast Streamer — FastAPI entry point.
Assembles routes, initializes DB, seeds settings, manages stream lifecycle.
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
from backend.models import User
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
    """Lightweight SQLite migrations for upgrades — add missing columns."""
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
            db.execute(text(f"SELECT {column} FROM {table} LIMIT 1"))
        except Exception:
            logger.info("Migration: adding %s.%s", table, column)
            db.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
            db.commit()

    # Populate display_name from original_filename where null
    try:
        db.execute(text(
            "UPDATE assets SET display_name = original_filename "
            "WHERE display_name IS NULL"
        ))
        db.commit()
    except Exception:
        pass

    # Assign unowned assets to the first admin user
    try:
        db.execute(text(
            "UPDATE assets SET owner_id = ("
            "  SELECT id FROM users WHERE is_admin = 1 LIMIT 1"
            ") WHERE owner_id IS NULL"
        ))
        db.commit()
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Initializing database...")
    init_db()

    db = SessionLocal()
    try:
        _run_migrations(db)

        # Create default admin user if it doesn't exist
        if not db.query(User).filter(User.username == DEFAULT_ADMIN_USERNAME).first():
            db.add(User(username=DEFAULT_ADMIN_USERNAME,
                        hashed_password=hash_password(DEFAULT_ADMIN_PASSWORD),
                        is_active=True, is_admin=True))
            db.commit()
            logger.info("Created default admin user: %s", DEFAULT_ADMIN_USERNAME)

        # Seed default server settings
        settings_routes.seed_default_settings(db)

        # Apply persisted settings to runtime config
        settings_routes._apply_runtime_settings(db)
    finally:
        db.close()

    app.state.stream_manager = StreamManager(db_session_factory=SessionLocal)
    app.state.browser_manager = BrowserManager(db_session_factory=SessionLocal)

    # Restore browser sources that were running before restart
    try:
        await app.state.browser_manager.restore_sessions()
    except Exception as exc:
        logger.error("Browser session restore failed: %s", exc)

    logger.info("Multicast Streamer ready")
    yield

    # Shutdown
    logger.info("Stopping all streams and browser sources...")
    await app.state.stream_manager.stop_all()
    await app.state.browser_manager.stop_all()


app = FastAPI(title="Multicast Streamer",
              description="Media library + MPEG-TS multicast playout",
              version="1.1.0", lifespan=lifespan)

app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS,
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

app.include_router(auth_routes.router)
app.include_router(asset_routes.router)
app.include_router(stream_routes.router)
app.include_router(settings_routes.router)

# Serve React frontend
FRONTEND_BUILD = Path(__file__).parent.parent / "frontend" / "build"
if FRONTEND_BUILD.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_BUILD / "static"), name="static")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        if full_path.startswith("api/"):
            return None
        index = FRONTEND_BUILD / "index.html"
        return FileResponse(str(index)) if index.exists() else {"detail": "Frontend not built"}
else:
    @app.get("/")
    async def root():
        return {"message": "API running", "docs": "/docs", "note": "Frontend not built"}
