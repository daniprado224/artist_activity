import logging
import os
import sys
import time

import requests
from dotenv import load_dotenv
import json as jsonlib

from db import get_connection
from matching import normalize_for_matching

load_dotenv()

TICKETMASTER_BASE_URL = "https://app.ticketmaster.com/discovery/v2"
MIN_REQUEST_INTERVAL_SECONDS = 0.22  # slightly over 1/5s to leave margin
MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 1.0
SEED_FILE = os.path.join(os.path.dirname(__file__), "..", "seed", "seed_artists.json")

API_KEY = os.environ.get("TICKETMASTER_API_KEY")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("ingest_ticketmaster")

_last_request_time = 0.0


def _rate_limited_get(url: str, params: dict) -> requests.Response | None:
    global _last_request_time

    backoff = INITIAL_BACKOFF_SECONDS
    for attempt in range(1, MAX_RETRIES + 1):
        elapsed = time.monotonic() - _last_request_time
        if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
            time.sleep(MIN_REQUEST_INTERVAL_SECONDS - elapsed)

        try:
            response = requests.get(url, params=params, timeout=15)
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

        if response.status_code == 429 or response.status_code >= 500:
            logger.warning(
                "got HTTP %d on attempt %d/%d for %s -- backing off %.1fs",
                response.status_code, attempt, MAX_RETRIES, url, backoff,
            )
            time.sleep(backoff)
            backoff *= 2
            continue

        return response

    return None


def search_attraction(name: str) -> dict | None:
    response = _rate_limited_get(
        f"{TICKETMASTER_BASE_URL}/attractions.json",
        params={"keyword": name, "apikey": API_KEY, "size": 20},
    )
    if response is None:
        logger.error("attraction search failed for %r: exhausted retries", name)
        return None
    if response.status_code != 200:
        logger.error("attraction search failed for %r: HTTP %d", name, response.status_code)
        return None

    try:
        attractions = response.json().get("_embedded", {}).get("attractions", [])
    except ValueError as exc:
        logger.error("attraction search failed for %r: invalid JSON (%s)", name, exc)
        return None

    if not attractions:
        logger.warning("no Ticketmaster attraction results for %r", name)
        return None

    def is_music(attraction: dict) -> bool:
        segments = [c.get("segment", {}).get("name") for c in attraction.get("classifications", [])]
        return "Music" in segments

    # prefer an exact match over keyword-search ranking -- otherwise a
    # tribute act/cover band routinely outranks the real artist
    normalized_query = normalize_for_matching(name)
    exact_matches = [a for a in attractions if normalize_for_matching(a.get("name", "")) == normalized_query]
    if exact_matches:
        music_exact = [a for a in exact_matches if is_music(a)]
        return music_exact[0] if music_exact else exact_matches[0]

    music_matches = [a for a in attractions if is_music(a)]
    return music_matches[0] if music_matches else attractions[0]


def fetch_events(attraction_id: str, seed_name: str) -> list[dict]:
    response = _rate_limited_get(
        f"{TICKETMASTER_BASE_URL}/events.json",
        params={"attractionId": attraction_id, "apikey": API_KEY, "size": 200},
    )
    if response is None:
        logger.error(
            "event fetch failed for %r (attraction=%s): exhausted retries", seed_name, attraction_id
        )
        return []
    if response.status_code != 200:
        logger.error(
            "event fetch failed for %r (attraction=%s): HTTP %d",
            seed_name, attraction_id, response.status_code,
        )
        return []

    try:
        return response.json().get("_embedded", {}).get("events", [])
    except ValueError as exc:
        logger.error(
            "event fetch failed for %r (attraction=%s): invalid JSON (%s)", seed_name, attraction_id, exc
        )
        return []


def upsert_attraction(cur, attraction: dict, seed_name: str) -> None:
    attraction_id = attraction["id"]
    name = attraction.get("name", "")

    classifications = attraction.get("classifications", [])
    primary = classifications[0] if classifications else {}
    segment = primary.get("segment", {}).get("name")
    genre = primary.get("genre", {}).get("name")

    upcoming_events = attraction.get("upcomingEvents", {})
    event_count = upcoming_events.get("_total", 0) if isinstance(upcoming_events, dict) else 0

    cur.execute(
        """
        INSERT INTO artist_ticketmaster_source (
            tm_attraction_id, seed_name, name, tm_segment, tm_genre, event_count, raw_response, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (tm_attraction_id) DO UPDATE SET
            seed_name = EXCLUDED.seed_name,
            name = EXCLUDED.name,
            tm_segment = EXCLUDED.tm_segment,
            tm_genre = EXCLUDED.tm_genre,
            event_count = EXCLUDED.event_count,
            raw_response = EXCLUDED.raw_response,
            updated_at = now()
        """,
        (attraction_id, seed_name, name, segment, genre, event_count, jsonlib.dumps(attraction)),
    )


def upsert_event(cur, attraction_id: str, event: dict) -> None:
    event_id = event["id"]
    event_name = event.get("name")

    venues = event.get("_embedded", {}).get("venues", [])
    venue = venues[0] if venues else {}
    venue_name = venue.get("name")
    venue_city = venue.get("city", {}).get("name")
    venue_country = venue.get("country", {}).get("countryCode")

    dates = event.get("dates", {}).get("start", {})
    event_date = dates.get("localDate")
    event_time = dates.get("localTime")
    is_date_tba = bool(dates.get("dateTBA") or dates.get("dateTBD") or not event_date)

    price_ranges = event.get("priceRanges", [])
    price_range = price_ranges[0] if price_ranges else {}
    price_min = price_range.get("min")
    price_max = price_range.get("max")
    currency = price_range.get("currency")

    cur.execute(
        """
        INSERT INTO ticketmaster_source_events (
            tm_event_id, tm_attraction_id, event_name, venue_name, venue_city, venue_country,
            event_date, event_time, is_date_tba, price_min, price_max, currency, raw_response, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (tm_event_id) DO UPDATE SET
            tm_attraction_id = EXCLUDED.tm_attraction_id,
            event_name = EXCLUDED.event_name,
            venue_name = EXCLUDED.venue_name,
            venue_city = EXCLUDED.venue_city,
            venue_country = EXCLUDED.venue_country,
            event_date = EXCLUDED.event_date,
            event_time = EXCLUDED.event_time,
            is_date_tba = EXCLUDED.is_date_tba,
            price_min = EXCLUDED.price_min,
            price_max = EXCLUDED.price_max,
            currency = EXCLUDED.currency,
            raw_response = EXCLUDED.raw_response,
            updated_at = now()
        """,
        (
            event_id, attraction_id, event_name, venue_name, venue_city, venue_country,
            event_date, event_time, is_date_tba, price_min, price_max, currency, jsonlib.dumps(event),
        ),
    )


def main() -> int:
    if not API_KEY:
        logger.error("TICKETMASTER_API_KEY is not set (check your .env) -- aborting")
        return 1

    with open(SEED_FILE, "r", encoding="utf-8") as f:
        seed_artists = jsonlib.load(f)

    conn = get_connection()
    conn.autocommit = False

    succeeded = 0
    failed = []

    for entry in seed_artists:
        seed_name = entry["seed_name"]
        try:
            attraction = search_attraction(seed_name)
            if attraction is None:
                failed.append(seed_name)
                continue

            events = fetch_events(attraction["id"], seed_name)

            with conn.cursor() as cur:
                upsert_attraction(cur, attraction, seed_name)
                for event in events:
                    upsert_event(cur, attraction["id"], event)
            conn.commit()
            succeeded += 1
            logger.info(
                "ingested %r -> attraction_id=%s (matched name=%r, %d events)",
                seed_name, attraction["id"], attraction.get("name"), len(events),
            )

        except Exception as exc:  # noqa: BLE001
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
