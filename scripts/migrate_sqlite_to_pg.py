#!/usr/bin/env python3
"""
One-time migration script: SQLite → PostgreSQL.

Copies all data from the old SQLite database to the new PostgreSQL database.
Run this once after deploying the PostgreSQL version, before starting the service.

Usage:
    python3 scripts/migrate_sqlite_to_pg.py [sqlite_path] [pg_url]

Defaults:
    sqlite_path: /opt/multicast-streamer/db/streamer.db
    pg_url:      postgresql://mcs:mcs@localhost:5432/mediacaster

Prerequisites:
    - PostgreSQL database created and Alembic migrations applied
    - SQLite database file accessible
    - psycopg2-binary installed
"""

import sys
import sqlite3
import os

# Add project root to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


# Tables in dependency order (foreign keys reference earlier tables)
TABLES = [
    "users",
    "assets",
    "streams",
    "stream_items",
    "browser_sources",
    "user_stream_assignments",
    "server_settings",
]

# Tables with auto-incrementing integer primary keys that need sequence resets
SEQUENCE_TABLES = {
    "users": "id",
    "assets": "id",
    "streams": "id",
    "stream_items": "id",
    "browser_sources": "id",
    "user_stream_assignments": "id",
}


def migrate(sqlite_path: str, pg_url: str):
    """Copy all rows from SQLite to PostgreSQL, table by table."""

    if not os.path.exists(sqlite_path):
        print(f"ERROR: SQLite database not found at {sqlite_path}")
        sys.exit(1)

    print(f"Source:  {sqlite_path}")
    print(f"Target:  {pg_url}")
    print()

    # Connect to SQLite
    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row

    # Connect to PostgreSQL
    pg_engine = create_engine(pg_url)
    PgSession = sessionmaker(bind=pg_engine)
    pg_session = PgSession()

    try:
        for table_name in TABLES:
            # Read all rows from SQLite
            try:
                cursor = sqlite_conn.execute(f"SELECT * FROM {table_name}")
            except sqlite3.OperationalError as e:
                print(f"  SKIP {table_name}: {e}")
                continue

            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]

            if not rows:
                print(f"  {table_name}: 0 rows (empty)")
                continue

            # Clear existing data in PostgreSQL (in case of re-run)
            pg_session.execute(text(f"DELETE FROM {table_name}"))

            # Insert rows into PostgreSQL
            placeholders = ", ".join([f":{col}" for col in columns])
            insert_sql = f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({placeholders})"

            for row in rows:
                row_dict = {col: row[col] for col in columns}
                # Convert SQLite boolean integers (0/1) to Python bools for PostgreSQL
                for key, val in row_dict.items():
                    if isinstance(val, int) and key.startswith(("is_", "must_", "capture_")):
                        row_dict[key] = bool(val)
                pg_session.execute(text(insert_sql), row_dict)

            print(f"  {table_name}: {len(rows)} rows migrated")

        # Reset PostgreSQL sequences to match the max ID in each table.
        # Without this, the next INSERT would get id=1 and conflict.
        for table_name, pk_col in SEQUENCE_TABLES.items():
            seq_name = f"{table_name}_{pk_col}_seq"
            try:
                pg_session.execute(text(
                    f"SELECT setval('{seq_name}', COALESCE((SELECT MAX({pk_col}) FROM {table_name}), 0) + 1, false)"
                ))
            except Exception as e:
                print(f"  WARNING: Could not reset sequence {seq_name}: {e}")

        pg_session.commit()
        print()
        print("Migration complete!")

    except Exception as e:
        pg_session.rollback()
        print(f"\nERROR: Migration failed: {e}")
        raise
    finally:
        pg_session.close()
        sqlite_conn.close()


if __name__ == "__main__":
    sqlite_path = sys.argv[1] if len(sys.argv) > 1 else "/opt/multicast-streamer/db/streamer.db"
    pg_url = sys.argv[2] if len(sys.argv) > 2 else "postgresql://mcs:mcs@localhost:5432/mediacaster"
    migrate(sqlite_path, pg_url)
