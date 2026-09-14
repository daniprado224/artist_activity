import csv
import logging
import os
import re
import sys

from rapidfuzz import fuzz

from db import get_connection
from matching import normalize_for_matching

MIN_CANDIDATE_THRESHOLD = 60.0   # below this, don't even record a candidate
SAFE_CONFIDENCE_THRESHOLD = 90.0  # below this, flag for manual review

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "output")
REVIEW_CSV_PATH = os.path.join(OUTPUT_DIR, "manual_review_queue.csv")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("resolve_entities")

_PUNCTUATION_RE = re.compile(r"[^\w\s]")


def normalize_name(name: str) -> str:
    folded = normalize_for_matching(name)
    no_punct = _PUNCTUATION_RE.sub("", folded)
    return re.sub(r"\s+", " ", no_punct).strip()


def load_mb_artists(cur) -> list[dict]:
    cur.execute("SELECT mbid, name FROM artist_musicbrainz_source ORDER BY name")
    return [{"mbid": row[0], "name": row[1]} for row in cur.fetchall()]


def load_tm_attractions(cur) -> list[dict]:
    cur.execute("SELECT tm_attraction_id, name FROM artist_ticketmaster_source ORDER BY name")
    return [{"tm_attraction_id": row[0], "name": row[1]} for row in cur.fetchall()]


def find_match(mb_name: str, tm_attractions: list[dict]) -> tuple[dict | None, float, str]:
    normalized_mb = normalize_name(mb_name)

    exact_candidates = [a for a in tm_attractions if normalize_name(a["name"]) == normalized_mb]
    if exact_candidates:
        if len(exact_candidates) > 1:
            logger.warning(
                "%r has %d exact-name matches on the Ticketmaster side; using the first",
                mb_name, len(exact_candidates),
            )
        return exact_candidates[0], 100.0, "exact_name"

    best_attraction = None
    best_score = -1.0
    for attraction in tm_attractions:
        score = fuzz.token_sort_ratio(mb_name, attraction["name"])
        if score > best_score:
            best_score = score
            best_attraction = attraction

    if best_attraction is None or best_score < MIN_CANDIDATE_THRESHOLD:
        return None, 0.0, ""

    return best_attraction, float(best_score), "fuzzy_token_sort_ratio"


def upsert_resolution(cur, mbid: str, tm_attraction_id: str, confidence: float, method: str) -> None:
    cur.execute(
        """
        INSERT INTO entity_resolution_map (mbid, tm_attraction_id, match_confidence, match_method)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (mbid, tm_attraction_id) DO UPDATE SET
            match_confidence = EXCLUDED.match_confidence,
            match_method = EXCLUDED.match_method
        """,
        (mbid, tm_attraction_id, round(confidence, 2), method),
    )


def main() -> int:
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    conn = get_connection()
    conn.autocommit = False

    with conn.cursor() as cur:
        mb_artists = load_mb_artists(cur)
        tm_attractions = load_tm_attractions(cur)

    if not mb_artists or not tm_attractions:
        logger.error(
            "nothing to resolve: %d MusicBrainz artists, %d Ticketmaster attractions in the database "
            "(did you run both ingestion scripts first?)",
            len(mb_artists), len(tm_attractions),
        )
        conn.close()
        return 1

    review_rows = []
    matched = 0
    unmatched = []

    for mb_artist in mb_artists:
        try:
            attraction, confidence, method = find_match(mb_artist["name"], tm_attractions)
            if attraction is None:
                unmatched.append(mb_artist["name"])
                logger.warning("no candidate above threshold for MusicBrainz artist %r", mb_artist["name"])
                continue

            with conn.cursor() as cur:
                upsert_resolution(cur, mb_artist["mbid"], attraction["tm_attraction_id"], confidence, method)
            conn.commit()
            matched += 1

            logger.info(
                "%r -> %r (confidence=%.2f, method=%s)",
                mb_artist["name"], attraction["name"], confidence, method,
            )

            if confidence < SAFE_CONFIDENCE_THRESHOLD:
                review_rows.append(
                    {
                        "mb_name": mb_artist["name"],
                        "mb_mbid": mb_artist["mbid"],
                        "tm_name": attraction["name"],
                        "tm_attraction_id": attraction["tm_attraction_id"],
                        "match_confidence": round(confidence, 2),
                        "match_method": method,
                    }
                )

        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            logger.error("unexpected error resolving %r: %s", mb_artist["name"], exc)

    conn.close()

    with open(REVIEW_CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["mb_name", "mb_mbid", "tm_name", "tm_attraction_id", "match_confidence", "match_method"],
        )
        writer.writeheader()
        writer.writerows(review_rows)

    logger.info(
        "done: %d matched (%d below safe threshold %.0f -> %s), %d unmatched out of %d MusicBrainz artists",
        matched, len(review_rows), SAFE_CONFIDENCE_THRESHOLD, REVIEW_CSV_PATH, len(unmatched), len(mb_artists),
    )
    if unmatched:
        logger.warning("unmatched MusicBrainz artists: %s", ", ".join(unmatched))

    return 0


if __name__ == "__main__":
    sys.exit(main())
