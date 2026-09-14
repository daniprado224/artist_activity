import json as jsonlib
import os
import random
import sys

from db import get_connection

SEED_FILE = os.path.join(os.path.dirname(__file__), "..", "seed", "seed_artists.json")
SAMPLE_SIZE = 20

TABLES = [
    "genres",
    "artist_musicbrainz_source",
    "musicbrainz_source_genres",
    "artist_ticketmaster_source",
    "ticketmaster_source_events",
    "entity_resolution_map",
    "artists",
    "events",
]

MB_NULL_CHECK_COLUMNS = [
    "disambiguation", "artist_type", "country",
    "began_active_year", "began_active_month", "began_active_day",
]
TM_NULL_CHECK_COLUMNS = ["tm_segment", "tm_genre"]
TM_EVENT_NULL_CHECK_COLUMNS = [
    "venue_name", "venue_city", "venue_country", "event_date", "price_min", "price_max", "currency",
]


def print_header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def report_row_counts(cur) -> None:
    print_header("ROW COUNTS")
    for table in TABLES:
        cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608
        print(f"  {table:<32} {cur.fetchone()[0]}")


def report_null_rates(cur, table: str, columns: list[str]) -> None:
    cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608
    total = cur.fetchone()[0]
    print(f"\n  {table} (n={total}):")
    if total == 0:
        print("    (no rows)")
        return
    for column in columns:
        cur.execute(f"SELECT count(*) FROM {table} WHERE {column} IS NULL")  # noqa: S608
        nulls = cur.fetchone()[0]
        print(f"    {column:<20} {nulls}/{total} null ({100.0 * nulls / total:.1f}%)")


def report_seed_resolution_gaps(cur) -> None:
    print_header("SEED ARTIST COVERAGE")

    with open(SEED_FILE, "r", encoding="utf-8") as f:
        seed_artists = jsonlib.load(f)
    seed_names = [entry["seed_name"] for entry in seed_artists]

    cur.execute("SELECT seed_name, mbid FROM artist_musicbrainz_source WHERE seed_name IS NOT NULL")
    mb_by_seed = dict(cur.fetchall())

    cur.execute("SELECT seed_name, tm_attraction_id FROM artist_ticketmaster_source WHERE seed_name IS NOT NULL")
    tm_by_seed = dict(cur.fetchall())

    cur.execute("SELECT mbid, tm_attraction_id FROM entity_resolution_map")
    resolved_pairs = set(cur.fetchall())

    missing_mb = [n for n in seed_names if n not in mb_by_seed]
    missing_tm = [n for n in seed_names if n not in tm_by_seed]

    both_sides = [n for n in seed_names if n in mb_by_seed and n in tm_by_seed]
    unresolved_same_pair = [
        n for n in both_sides
        if (mb_by_seed[n], tm_by_seed[n]) not in resolved_pairs
    ]

    print(f"  seed artists total:                              {len(seed_names)}")
    print(f"  missing on MusicBrainz side (ingestion failure):  {len(missing_mb)}")
    if missing_mb:
        print(f"    -> {', '.join(missing_mb)}")
    print(f"  missing on Ticketmaster side (ingestion failure): {len(missing_tm)}")
    if missing_tm:
        print(f"    -> {', '.join(missing_tm)}")
    print(f"  present on both sides but NOT linked to each other")
    print(f"  by entity_resolution_map (resolution failure):    {len(unresolved_same_pair)}")
    if unresolved_same_pair:
        print(f"    -> {', '.join(unresolved_same_pair)}")

    failed_either_side = set(missing_mb) | set(missing_tm) | set(unresolved_same_pair)
    print(f"\n  TOTAL seed artists that failed to resolve end-to-end: {len(failed_either_side)}/{len(seed_names)}")


def print_sample(cur) -> None:
    print_header(f"RANDOM SAMPLE OF UP TO {SAMPLE_SIZE} RESOLVED MATCHES")

    cur.execute("SELECT resolution_id, mbid, tm_attraction_id FROM entity_resolution_map")
    all_ids = cur.fetchall()
    if not all_ids:
        print("  (entity_resolution_map is empty -- run resolve_entities.py first)")
        return

    sample = random.sample(all_ids, min(SAMPLE_SIZE, len(all_ids)))

    for resolution_id, mbid, tm_attraction_id in sample:
        cur.execute(
            "SELECT name FROM artist_musicbrainz_source WHERE mbid = %s", (mbid,)
        )
        mb_name = cur.fetchone()[0]

        cur.execute(
            """
            SELECT g.genre_name FROM musicbrainz_source_genres msg
            JOIN genres g ON g.genre_id = msg.genre_id
            WHERE msg.mbid = %s
            ORDER BY msg.tag_count DESC NULLS LAST
            LIMIT 3
            """,
            (mbid,),
        )
        mb_genres = ", ".join(row[0] for row in cur.fetchall()) or "(no tags)"

        cur.execute(
            "SELECT name FROM artist_ticketmaster_source WHERE tm_attraction_id = %s", (tm_attraction_id,)
        )
        tm_name = cur.fetchone()[0]

        cur.execute(
            """
            SELECT venue_name FROM ticketmaster_source_events
            WHERE tm_attraction_id = %s AND venue_name IS NOT NULL
            ORDER BY event_date
            LIMIT 1
            """,
            (tm_attraction_id,),
        )
        venue_row = cur.fetchone()
        tm_venue = venue_row[0] if venue_row else "(no event/venue data)"

        cur.execute(
            "SELECT match_confidence, match_method FROM entity_resolution_map WHERE resolution_id = %s",
            (resolution_id,),
        )
        confidence, method = cur.fetchone()

        print(f"\n  [{resolution_id}] confidence={confidence} method={method}")
        print(f"    MusicBrainz : {mb_name!r:40} genres: {mb_genres}")
        print(f"    Ticketmaster: {tm_name!r:40} venue:  {tm_venue}")


def main() -> int:
    conn = get_connection()
    with conn.cursor() as cur:
        print_sample(cur)
        print_header("INGESTION QA")
        report_row_counts(cur)
        report_null_rates(cur, "artist_musicbrainz_source", MB_NULL_CHECK_COLUMNS)
        report_null_rates(cur, "artist_ticketmaster_source", TM_NULL_CHECK_COLUMNS)
        report_null_rates(cur, "ticketmaster_source_events", TM_EVENT_NULL_CHECK_COLUMNS)
        report_seed_resolution_gaps(cur)
    conn.close()
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
