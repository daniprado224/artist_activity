import os

import psycopg2
from dotenv import load_dotenv

load_dotenv()


def get_connection():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "artist_activity"),
        user=os.environ.get("POSTGRES_USER", "artist_activity"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
    )


def get_or_create_genre_id(cur, genre_name: str) -> int:
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
