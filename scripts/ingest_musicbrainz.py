"""Ingest raw artist data from the MusicBrainz API for the seed artist list.

For each seed name:
  1. GET /ws/2/artist/?query=... to find the best-scoring artist match.
  2. GET /ws/2/artist/{mbid}?inc=tags+release-groups to pull life-span,
     folksonomy tags, and a release-group count for that artist.
  3. Upsert into artist_musicbrainz_source (+ musicbrainz_source_genres).

Rate limiting: MusicBrainz's stated limit is 1 request/second per IP for
unauthenticated use. We sleep at least MIN_REQUEST_INTERVAL_SECONDS between
every single HTTP call (not just per-artist -- each artist costs 2 calls),
and back off with increasing delay on 503 (their "you're being rate
limited" response) or any 5xx, up to MAX_RETRIES attempts.

A failure on one artist (no match found, HTTP error after retries, a
malformed response) is logged with the artist name and status code and
the script moves to the next artist -- it never aborts the whole batch.

Idempotent: mbid is the primary key, so INSERT ... ON CONFLICT (mbid) DO
UPDATE means re-running this script updates existing rows in place instead
of duplicating them.
"""
import logging
import os
import sys
import time

import requests
from dotenv import load_dotenv
import json as jsonlib

from db import get_connection, get_or_create_genre_id

load_dotenv()

MUSICBRAINZ_BASE_URL = "https://musicbrainz.org/ws/2"
MIN_REQUEST_INTERVAL_SECONDS = 1.05  # slightly over 1 req/sec to leave margin
MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 2.0
SEED_FILE = os.path.join(os.path.dirname(__file__), "..", "seed", "seed_artists.json")

USER_AGENT = os.environ.get(
    "MUSICBRAINZ_USER_AGENT",
    "ArtistActivityDataLayer/0.1 ( set MUSICBRAINZ_USER_AGENT in .env )",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("ingest_musicbrainz")

_last_request_time = 0.0


def _rate_limited_get(url: str, params: dict) -> requests.Response | None:
    """GET with the shared 1 req/sec pacing and 5xx backoff/retry.

    Returns None (after logging) if every retry is exhausted -- callers
    treat that as "this artist failed", not as a reason to crash.
    """
    global _last_request_time

    backoff = INITIAL_BACKOFF_SECONDS
    for attempt in range(1, MAX_RETRIES + 1):
        elapsed = time.monotonic() - _last_request_time
        if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
            time.sleep(MIN_REQUEST_INTERVAL_SECONDS - elapsed)

        try:
            response = requests.get(
                url, params=params, headers={"User-Agent": USER_AGENT}, timeout=15
            )
        except requests.RequestException as exc:
            logger.warning(
                "network error on attempt %d/%d for %s: %s", attempt, MAX_RETRIES, url, exc
            )
            _last_request_time = time.monotonic()
            time.sleep(backoff)
            backoff *= 2
            continue

        _last_request_time = time.monotonic()

        if response.status_code == 200:
            return response

        if response.status_code == 503 or response.status_code >= 500:
            logger.warning(
                "got HTTP %d on attempt %d/%d for %s -- backing off %.1fs",
                response.status_code, attempt, MAX_RETRIES, url, backoff,
            )
            time.sleep(backoff)
            backoff *= 2
            continue

        # Non-retryable status (4xx other than 429-style rate limiting).
        return response

    return None


def search_artist(name: str) -> dict | None:
    """Return the best MB artist search result for `name`, or None.

    Prefers an exact (case-insensitive) name match over MusicBrainz's own
    relevance score. MB's scoring will otherwise happily rank an unrelated
    artist whose name merely contains the query above the actual artist
    (observed in practice: querying "Phoenix" scored "Nick Phoenix" above
    the band Phoenix; querying "Kanye West" scored "Kanye West Tribute
    Band" above the real Kanye West). This only helps when the correct
    artist IS present among the results but wasn't top-scored -- if MB
    has no exact-name entry at all, or the correct entry didn't make the
    top `limit` results, this does nothing. See README known-limitations.
    """
    response = _rate_limited_get(
        f"{MUSICBRAINZ_BASE_URL}/artist/",
        params={"query": f'artist:"{name}"', "fmt": "json", "limit": 10},
    )
    if response is None:
        logger.error("search failed for %r: exhausted retries", name)
        return None
    if response.status_code != 200:
        logger.error("search failed for %r: HTTP %d", name, response.status_code)
        return None

    try:
        artists = response.json().get("artists", [])
    except ValueError as exc:
        logger.error("search failed for %r: invalid JSON (%s)", name, exc)
        return None

    if not artists:
        logger.warning("no MusicBrainz search results for %r", name)
        return None

    normalized_query = name.strip().lower()
    exact_matches = [a for a in artists if a.get("name", "").strip().lower() == normalized_query]
    if exact_matches:
        return max(exact_matches, key=lambda a: a.get("score", 0))

    return max(artists, key=lambda a: a.get("score", 0))


def lookup_artist_detail(mbid: str, seed_name: str) -> dict | None:
    """Fetch full artist detail (tags + release-groups) for a known MBID."""
    response = _rate_limited_get(
        f"{MUSICBRAINZ_BASE_URL}/artist/{mbid}",
        params={"inc": "tags+release-groups", "fmt": "json"},
    )
    if response is None:
        logger.error("detail lookup failed for %r (mbid=%s): exhausted retries", seed_name, mbid)
        return None
    if response.status_code != 200:
        logger.error(
            "detail lookup failed for %r (mbid=%s): HTTP %d", seed_name, mbid, response.status_code
        )
        return None

    try:
        return response.json()
    except ValueError as exc:
        logger.error("detail lookup failed for %r (mbid=%s): invalid JSON (%s)", seed_name, mbid, exc)
        return None


def parse_life_span_begin(life_span: dict | None) -> tuple[int | None, int | None, int | None, str | None]:
    """Split MB's partial 'YYYY', 'YYYY-MM', or 'YYYY-MM-DD' begin date into parts."""
    if not life_span:
        return None, None, None, None
    raw = life_span.get("begin")
    if not raw:
        return None, None, None, None

    parts = raw.split("-")
    year = int(parts[0]) if len(parts) >= 1 and parts[0].isdigit() else None
    month = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None
    day = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
    return year, month, day, raw


def upsert_artist(cur, detail: dict, seed_name: str) -> None:
    mbid = detail["id"]
    name = detail.get("name", "")
    disambiguation = detail.get("disambiguation") or None
    artist_type = detail.get("type") or None
    country = detail.get("country") or None

    year, month, day, raw_begin = parse_life_span_begin(detail.get("life-span"))
    release_count = len(detail.get("release-groups", []))

    cur.execute(
        """
        INSERT INTO artist_musicbrainz_source (
            mbid, seed_name, name, disambiguation, artist_type, country,
            began_active_year, began_active_month, began_active_day, began_active_raw,
            release_count, raw_response, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (mbid) DO UPDATE SET
            seed_name = EXCLUDED.seed_name,
            name = EXCLUDED.name,
            disambiguation = EXCLUDED.disambiguation,
            artist_type = EXCLUDED.artist_type,
            country = EXCLUDED.country,
            began_active_year = EXCLUDED.began_active_year,
            began_active_month = EXCLUDED.began_active_month,
            began_active_day = EXCLUDED.began_active_day,
            began_active_raw = EXCLUDED.began_active_raw,
            release_count = EXCLUDED.release_count,
            raw_response = EXCLUDED.raw_response,
            updated_at = now()
        """,
        (
            mbid, seed_name, name, disambiguation, artist_type, country,
            year, month, day, raw_begin,
            release_count, jsonlib.dumps(detail),
        ),
    )

    # Replace this artist's genre tags wholesale -- simpler and safer to
    # reason about than diffing old vs. new tag sets, and cheap at this scale.
    cur.execute("DELETE FROM musicbrainz_source_genres WHERE mbid = %s", (mbid,))
    for tag in detail.get("tags", []):
        tag_name = tag.get("name")
        if not tag_name:
            continue
        genre_id = get_or_create_genre_id(cur, tag_name)
        cur.execute(
            """
            INSERT INTO musicbrainz_source_genres (mbid, genre_id, tag_count)
            VALUES (%s, %s, %s)
            ON CONFLICT (mbid, genre_id) DO UPDATE SET tag_count = EXCLUDED.tag_count
            """,
            (mbid, genre_id, tag.get("count")),
        )


def main() -> int:
    with open(SEED_FILE, "r", encoding="utf-8") as f:
        seed_artists = jsonlib.load(f)

    conn = get_connection()
    conn.autocommit = False

    succeeded = 0
    failed = []

    for entry in seed_artists:
        seed_name = entry["seed_name"]
        try:
            best_match = search_artist(seed_name)
            if best_match is None:
                failed.append(seed_name)
                continue

            detail = lookup_artist_detail(best_match["id"], seed_name)
            if detail is None:
                failed.append(seed_name)
                continue

            with conn.cursor() as cur:
                upsert_artist(cur, detail, seed_name)
            conn.commit()
            succeeded += 1
            logger.info("ingested %r -> mbid=%s (matched name=%r)", seed_name, detail["id"], detail.get("name"))

        except Exception as exc:  # noqa: BLE001 -- one bad artist must not kill the batch
            conn.rollback()
            logger.error("unexpected error ingesting %r: %s", seed_name, exc)
            failed.append(seed_name)

    conn.close()

    logger.info("done: %d succeeded, %d failed out of %d seed artists", succeeded, len(failed), len(seed_artists))
    if failed:
        logger.warning("failed artists: %s", ", ".join(failed))

    return 0


if __name__ == "__main__":
    sys.exit(main())
