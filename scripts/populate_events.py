"""Phase 2, step 1: promote trustworthy entity_resolution_map rows into the
canonical artists/events tables.

A resolution row is "trustworthy enough to promote" if match_confidence >=
PROMOTION_THRESHOLD -- the same 90/100 cutoff resolve_entities.py already
uses to decide what needs manual review. In practice this means anything
already sitting in output/manual_review_queue.csv is correctly excluded,
not a gap: those rows were reviewed and are known-wrong tribute-act
matches, not borderline-but-probably-fine ones.

For each promoted row:
  1. Upsert an `artists` row keyed on mbid (canonical_name taken from the
     MusicBrainz name, since that's the artist's real/legal name rather
     than however Ticketmaster happens to list them).
  2. Upsert one `events` row per ticketmaster_source_events row for that
     attraction, tagged with the artist's single highest-tag-count genre
     (or NULL if the artist has no tags) -- see the schema's genre design
     note: events.genre_id is one canonical genre per event, not the full
     MusicBrainz M2M tag set.

Idempotent: artists is upserted on the mbid UNIQUE constraint; events is
upserted on the source_tm_event_id UNIQUE constraint. Re-running this
script after new ingestion/resolution data updates existing rows in
place rather than duplicating them. It does NOT delete an artists/events
row whose resolution_map entry later drops below threshold -- see README
known-limitations.
"""
import logging
import sys

from db import get_connection

PROMOTION_THRESHOLD = 90.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("populate_events")


def load_promotable_matches(cur) -> list[dict]:
    cur.execute(
        """
        SELECT r.mbid, r.tm_attraction_id, r.match_confidence, mb.name AS mb_name
        FROM entity_resolution_map r
        JOIN artist_musicbrainz_source mb ON mb.mbid = r.mbid
        WHERE r.match_confidence >= %s
        ORDER BY mb.name
        """,
        (PROMOTION_THRESHOLD,),
    )
    columns = [desc[0] for desc in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def upsert_artist(cur, mbid: str, tm_attraction_id: str, canonical_name: str) -> int:
    cur.execute(
        """
        INSERT INTO artists (mbid, tm_attraction_id, canonical_name, updated_at)
        VALUES (%s, %s, %s, now())
        ON CONFLICT (mbid) DO UPDATE SET
            tm_attraction_id = EXCLUDED.tm_attraction_id,
            canonical_name = EXCLUDED.canonical_name,
            updated_at = now()
        RETURNING artist_id
        """,
        (mbid, tm_attraction_id, canonical_name),
    )
    return cur.fetchone()[0]


def primary_genre_id(cur, mbid: str) -> int | None:
    """The artist's single highest-tag-count genre, or None if untagged."""
    cur.execute(
        """
        SELECT genre_id FROM musicbrainz_source_genres
        WHERE mbid = %s
        ORDER BY tag_count DESC NULLS LAST, genre_id ASC
        LIMIT 1
        """,
        (mbid,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def upsert_events_for_attraction(cur, artist_id: int, tm_attraction_id: str, genre_id: int | None) -> int:
    cur.execute(
        """
        SELECT tm_event_id, venue_name, venue_city, venue_country, event_date, price_min, price_max
        FROM ticketmaster_source_events
        WHERE tm_attraction_id = %s
        """,
        (tm_attraction_id,),
    )
    rows = cur.fetchall()

    for tm_event_id, venue_name, venue_city, venue_country, event_date, price_min, price_max in rows:
        cur.execute(
            """
            INSERT INTO events (
                artist_id, source_tm_event_id, venue_name, venue_city, venue_country,
                event_date, price_min, price_max, genre_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_tm_event_id) DO UPDATE SET
                artist_id = EXCLUDED.artist_id,
                venue_name = EXCLUDED.venue_name,
                venue_city = EXCLUDED.venue_city,
                venue_country = EXCLUDED.venue_country,
                event_date = EXCLUDED.event_date,
                price_min = EXCLUDED.price_min,
                price_max = EXCLUDED.price_max,
                genre_id = EXCLUDED.genre_id
            """,
            (
                artist_id, tm_event_id, venue_name, venue_city, venue_country,
                event_date, price_min, price_max, genre_id,
            ),
        )

    return len(rows)


def main() -> int:
    conn = get_connection()
    conn.autocommit = False

    with conn.cursor() as cur:
        matches = load_promotable_matches(cur)

    if not matches:
        logger.error(
            "no entity_resolution_map rows scored >= %.0f -- nothing to promote "
            "(did you run resolve_entities.py first?)",
            PROMOTION_THRESHOLD,
        )
        conn.close()
        return 1

    promoted = 0
    total_events = 0
    failed = []

    for match in matches:
        try:
            with conn.cursor() as cur:
                artist_id = upsert_artist(cur, match["mbid"], match["tm_attraction_id"], match["mb_name"])
                genre_id = primary_genre_id(cur, match["mbid"])
                event_count = upsert_events_for_attraction(cur, artist_id, match["tm_attraction_id"], genre_id)
            conn.commit()
            promoted += 1
            total_events += event_count
            logger.info(
                "promoted %r -> artist_id=%d (%d events, confidence=%.2f)",
                match["mb_name"], artist_id, event_count, match["match_confidence"],
            )
        except Exception as exc:  # noqa: BLE001 -- one bad row must not kill the batch
            conn.rollback()
            logger.error("unexpected error promoting %r: %s", match["mb_name"], exc)
            failed.append(match["mb_name"])

    conn.close()

    logger.info(
        "done: %d artists promoted (%d total events), %d failed out of %d candidates",
        promoted, total_events, len(failed), len(matches),
    )
    if failed:
        logger.warning("failed artists: %s", ", ".join(failed))

    return 0


if __name__ == "__main__":
    sys.exit(main())
