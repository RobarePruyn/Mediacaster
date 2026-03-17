"""
Database engine and session factory for SQLite.

This module creates the SQLAlchemy engine, session factory, and declarative
base that all models inherit from. SQLite is used as the database backend
for simplicity — the entire database is a single file on disk.

Key components:
  - ``engine``: The SQLAlchemy Engine connected to the SQLite file.
  - ``SessionLocal``: A session factory for creating per-request DB sessions.
  - ``Base``: The declarative base class that all ORM models inherit from.
  - ``get_db()``: A FastAPI dependency that provides a session per request
    and ensures it is closed after the response is sent.
  - ``init_db()``: Creates all tables defined by Base subclasses.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from backend.config import DATABASE_PATH

# SQLite connection URL — three slashes for absolute path (sqlite:///path)
DATABASE_URL = f"sqlite:///{DATABASE_PATH}"

engine = create_engine(
    DATABASE_URL,
    # SQLite requires this for multi-threaded access (FastAPI is async).
    # Without it, SQLite raises "ProgrammingError: SQLite objects created
    # in a thread can only be used in that same thread."
    connect_args={"check_same_thread": False},
    # Set echo=True to log all SQL statements (useful for debugging)
    echo=False,
)

# Session factory — autocommit=False means we control transactions explicitly.
# autoflush=False prevents automatic flushes before queries, giving us more
# predictable behavior (we flush/commit when we're ready).
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# All ORM models inherit from this base class, which provides the
# metadata registry used by create_all() and migration tooling.
Base = declarative_base()


def get_db():
    """
    FastAPI dependency that yields a database session per request.

    Usage in route handlers::

        @router.get("/example")
        def example(db: Session = Depends(get_db)):
            ...

    The session is automatically closed when the request completes,
    even if an exception occurs.

    Yields:
        Session: An active SQLAlchemy session bound to the SQLite engine.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """
    Create all tables that don't already exist in the database.

    Called once during application startup. SQLAlchemy inspects the
    database and only creates tables whose names are not yet present —
    it does NOT alter existing tables (that's handled by _run_migrations
    in main.py).
    """
    Base.metadata.create_all(bind=engine)
