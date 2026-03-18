"""
Database engine and session factory for PostgreSQL.

This module creates the SQLAlchemy engine, session factory, and declarative
base that all models inherit from. PostgreSQL is the production database.

Key components:
  - ``engine``: The SQLAlchemy Engine connected to PostgreSQL.
  - ``SessionLocal``: A session factory for creating per-request DB sessions.
  - ``Base``: The declarative base class that all ORM models inherit from.
  - ``get_db()``: A FastAPI dependency that provides a session per request
    and ensures it is closed after the response is sent.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from backend.config import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    # PostgreSQL connection pool: 5 persistent connections, up to 10 overflow
    # for burst traffic. Adequate for a single-worker uvicorn deployment.
    pool_size=5,
    max_overflow=10,
    # Recycle connections every 30 minutes to avoid stale connections
    # after PostgreSQL restarts or idle timeouts.
    pool_recycle=1800,
)

# Session factory — autocommit=False means we control transactions explicitly.
# autoflush=False prevents automatic flushes before queries, giving us more
# predictable behavior (we flush/commit when we're ready).
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# All ORM models inherit from this base class, which provides the
# metadata registry used by Alembic and create_all().
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
        Session: An active SQLAlchemy session bound to the PostgreSQL engine.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
