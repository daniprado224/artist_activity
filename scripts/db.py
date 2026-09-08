"""Shared Postgres connection helper for the ingestion/resolution/validation scripts.

Deliberately tiny: this is not a data-access layer, just one function so every
script gets its connection parameters from the same environment variables
instead of duplicating `os.environ` calls.
"""
import os

import psycopg2
from dotenv import load_dotenv

load_dotenv()


def get_connection():
    """Open a new psycopg2 connection using POSTGRES_* env vars.

    Raises psycopg2.OperationalError (uncaught) if the database is
    unreachable -- a script that can't connect at all should fail loudly
    on startup, not be silently retried like a single bad API response.
    """
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "artist_activity"),
        user=os.environ.get("POSTGRES_USER", "artist_activity"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
    )


def get_or_create_genre_id(cur, genre_name: str) -> int:
    """Look up genre_id for a (lowercased/trimmed) genre name, inserting it if new.

    Centralized here because three different scripts (MB ingestion,
    resolution CSV export, future events population) all need to turn a
    free-text genre string into the same shared genres.genre_id.
    """
    normalized = genre_name.strip().lower()
    cur.execute(
        """
        INSERT INTO genres (genre_name) VALUES (%s)
        ON CONFLICT (genre_name) DO UPDATE SET genre_name = EXCLUDED.genre_name
        RETURNING genre_id
        """,
        (normalized,),
    )
    return cur.fetchone()[0]
