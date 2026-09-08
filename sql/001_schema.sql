-- =====================================================================
-- Phase 1 schema: artist catalog (MusicBrainz) vs. live activity
-- (Ticketmaster) data layer.
--
-- Design notes that apply to the whole file:
--   * Natural keys are used wherever the source system already hands us
--     a stable, globally-unique identifier (MusicBrainz MBIDs are real
--     UUIDs; Ticketmaster attraction/event IDs are opaque but unique
--     strings). Surrogate GENERATED IDENTITY keys are only introduced
--     for rows we mint ourselves (artists, genres, entity_resolution_map,
--     events), where there is no natural key to lean on.
--   * Every raw source table keeps a `raw_response JSONB` column. The
--     structured columns are what we query against; the JSONB is kept
--     so a later change to what we extract doesn't require re-hitting
--     the rate-limited APIs to backfill a new column.
--   * `ingested_at` is set once (default now()); `updated_at` is bumped
--     by the ingestion scripts on every upsert so we can see staleness
--     per row. No triggers are used for this on purpose: the scripts do
--     one explicit UPSERT statement each, and a hand-reviewed schema
--     should not hide behavior in trigger functions.
-- =====================================================================

-- ---------------------------------------------------------------------
-- genres: shared lookup table for genre/tag strings.
--
-- Both MusicBrainz (folksonomy tags on artists) and our normalized
-- events table need a "genre" concept. Rather than storing the same
-- free-text genre string redundantly in multiple tables (which invites
-- drift -- "indie rock" in one row, "Indie Rock" in another), every
-- genre string is interned here exactly once and referenced by FK.
--
-- This does NOT solve MusicBrainz's tag messiness (e.g. "indie rock" vs
-- "indie-rock" vs "indierock" are three different folksonomy tags in
-- their data and will land as three distinct rows here unless someone
-- curates an alias table later). See README "Known limitations".
-- ---------------------------------------------------------------------
CREATE TABLE genres (
    genre_id    INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Stored lowercased/trimmed by the loading code so "Rock" and "rock"
    -- collide on the UNIQUE constraint instead of silently duplicating.
    genre_name  TEXT NOT NULL UNIQUE
);

-- ---------------------------------------------------------------------
-- artist_musicbrainz_source: raw ingested MusicBrainz artist data.
--
-- PK is the MBID itself (a UUID MusicBrainz already guarantees is
-- globally unique and stable) rather than a surrogate key -- there is
-- no reason to mint a second identifier for a row that already has one.
--
-- Life-span dates from MusicBrainz are frequently partial ("just a
-- year", or "year-month, no day") -- MB itself models them this way
-- internally. We store year/month/day as separate nullable columns
-- (per your decision) instead of forcing them into a single DATE, so
-- "began active in 1990" is never fabricated into "1990-01-01" -- a
-- literal reading of a padded DATE would be a false precision claim.
-- `began_active_raw` keeps the untouched API string as a fallback for
-- any format MB returns that the year/month/day parser doesn't expect.
-- ---------------------------------------------------------------------
CREATE TABLE artist_musicbrainz_source (
    mbid                UUID PRIMARY KEY,
    -- Which seed_artists.json entry produced this row, kept purely for
    -- ingestion traceability/QA (e.g. "did seed artist X fail to
    -- resolve on the MB side at all?"). Not part of MusicBrainz's own
    -- data model, and not unique: a seed name could in principle map to
    -- an MBID also reachable from a different seed entry.
    seed_name           TEXT,
    name                TEXT NOT NULL,
    disambiguation      TEXT,
    artist_type         TEXT,      -- MB "type": Person, Group, Orchestra, etc.
    country             TEXT,      -- ISO 3166-1 alpha-2, as MB returns it.

    began_active_year   SMALLINT,
    began_active_month  SMALLINT CHECK (began_active_month BETWEEN 1 AND 12),
    began_active_day    SMALLINT CHECK (began_active_day BETWEEN 1 AND 31),
    began_active_raw    TEXT,      -- untouched "life-span.begin" string from MB.

    -- Count of release-groups (albums/EPs/etc, not individual reissues)
    -- attributed to this artist at ingestion time. Never negative.
    release_count       INTEGER NOT NULL DEFAULT 0 CHECK (release_count >= 0),

    raw_response        JSONB,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- A month is meaningless without a year, and a day is meaningless
    -- without a month -- this rules out storing "day 15" with no month,
    -- which MB's own data model would never produce either.
    CHECK (began_active_month IS NULL OR began_active_year IS NOT NULL),
    CHECK (began_active_day IS NULL OR began_active_month IS NOT NULL)
);

CREATE INDEX idx_mb_source_name ON artist_musicbrainz_source (lower(name));
CREATE INDEX idx_mb_source_seed_name ON artist_musicbrainz_source (seed_name);

-- ---------------------------------------------------------------------
-- musicbrainz_source_genres: M2M junction between a raw MB artist row
-- and the genres lookup. An artist can (and usually does) carry many
-- folksonomy tags, so this cannot be a single FK column on the source
-- table -- it would violate 1NF (a repeating group in disguise).
--
-- `tag_count` preserves the MusicBrainz tag vote count, which is a
-- real signal of how strongly that tag applies vs. being a one-off
-- user tag; it is nullable because not every ingestion path surfaces it.
-- ---------------------------------------------------------------------
CREATE TABLE musicbrainz_source_genres (
    mbid        UUID NOT NULL REFERENCES artist_musicbrainz_source (mbid) ON DELETE CASCADE,
    genre_id    INTEGER NOT NULL REFERENCES genres (genre_id) ON DELETE CASCADE,
    tag_count   INTEGER CHECK (tag_count >= 0),
    PRIMARY KEY (mbid, genre_id)
);

-- ---------------------------------------------------------------------
-- artist_ticketmaster_source: raw ingested Ticketmaster ATTRACTION data
-- only (one row per performer/act, not per show).
--
-- Per your decision, venue/price/date fields do NOT live here: those
-- vary per individual event, and cramming them into this table (as
-- arrays or JSON) would break 1NF and make idempotent per-event upserts
-- impossible. They live in `ticketmaster_source_events` below, one row
-- per real Ticketmaster event, FK'd back to this table.
--
-- PK is the Ticketmaster attraction ID as TM assigns it -- an opaque
-- alphanumeric string (e.g. "K8vZ9171oC0"), not a UUID, so it is typed
-- TEXT rather than UUID.
-- ---------------------------------------------------------------------
CREATE TABLE artist_ticketmaster_source (
    tm_attraction_id    TEXT PRIMARY KEY,
    -- Same purpose as artist_musicbrainz_source.seed_name -- see comment there.
    seed_name           TEXT,
    name                TEXT NOT NULL,
    -- Ticketmaster's own classification segment/genre strings (e.g.
    -- segment "Music", genre "Rock"). Kept as raw TM vocabulary here,
    -- not normalized against the shared `genres` table -- this is the
    -- *raw* source table; normalization happens once at the `events`
    -- level, where a single canonical genre_id is assigned.
    tm_segment          TEXT,
    tm_genre            TEXT,

    -- Total event count TM reports for this attraction at ingestion
    -- time (may include past + future events depending on the API
    -- response; see README limitations on what this does/doesn't cover).
    event_count         INTEGER NOT NULL DEFAULT 0 CHECK (event_count >= 0),

    raw_response        JSONB,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_tm_source_name ON artist_ticketmaster_source (lower(name));
CREATE INDEX idx_tm_source_seed_name ON artist_ticketmaster_source (seed_name);

-- ---------------------------------------------------------------------
-- ticketmaster_source_events: one row per real Ticketmaster event
-- (a specific show, on a specific date, at a specific venue) for an
-- attraction. This is the child table introduced by the attraction
-- grain decision above.
--
-- PK is the Ticketmaster event ID (natural key, opaque string).
-- `is_date_tba` exists because Ticketmaster genuinely publishes events
-- with an announced attraction/venue but no confirmed date yet
-- ("date to be announced") -- treating that as a NULL event_date with
-- a flag is more honest than omitting the row or guessing a date.
-- ---------------------------------------------------------------------
CREATE TABLE ticketmaster_source_events (
    tm_event_id         TEXT PRIMARY KEY,
    tm_attraction_id    TEXT NOT NULL REFERENCES artist_ticketmaster_source (tm_attraction_id) ON DELETE CASCADE,

    event_name          TEXT,
    venue_name          TEXT,
    venue_city          TEXT,
    venue_country       TEXT,       -- ISO 3166-1 alpha-2.

    event_date          DATE,
    event_time          TIME,       -- local venue time, when TM provides it.
    is_date_tba         BOOLEAN NOT NULL DEFAULT false,

    price_min           NUMERIC(10, 2) CHECK (price_min >= 0),
    price_max           NUMERIC(10, 2) CHECK (price_max >= 0),
    currency            CHAR(3),    -- ISO 4217, e.g. 'USD'.

    raw_response        JSONB,
    ingested_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    CHECK (price_max IS NULL OR price_min IS NULL OR price_max >= price_min)
);

CREATE INDEX idx_tm_events_attraction ON ticketmaster_source_events (tm_attraction_id);
CREATE INDEX idx_tm_events_date ON ticketmaster_source_events (event_date);

-- ---------------------------------------------------------------------
-- entity_resolution_map: every candidate MBID <-> TM attraction ID
-- match considered by resolve_entities.py, exact or fuzzy, kept as a
-- permanent audit trail -- this is deliberately NOT limited to accepted
-- matches, so you can see what was compared and why something did or
-- didn't get linked.
--
-- UNIQUE (mbid, tm_attraction_id) is what makes re-running the resolver
-- idempotent: re-resolving the same pair updates the existing row
-- (confidence/method may change as the algorithm improves) instead of
-- inserting a duplicate candidate.
--
-- `manually_verified` defaults to false and is NEVER set to true by the
-- automated resolver, regardless of confidence -- a high automated
-- confidence score is not the same claim as "a human looked at this and
-- confirmed it," and the schema should not blur that distinction.
-- Downstream consumers decide their own "safe to use automatically"
-- confidence cutoff by reading `match_confidence` directly.
-- ---------------------------------------------------------------------
CREATE TABLE entity_resolution_map (
    resolution_id       INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    mbid                UUID NOT NULL REFERENCES artist_musicbrainz_source (mbid) ON DELETE CASCADE,
    tm_attraction_id    TEXT NOT NULL REFERENCES artist_ticketmaster_source (tm_attraction_id) ON DELETE CASCADE,

    -- 0-100 scale. Exact matches are recorded as exactly 100.00 rather
    -- than a special sentinel, so ORDER BY match_confidence is always
    -- meaningful across both methods.
    match_confidence    NUMERIC(5, 2) NOT NULL CHECK (match_confidence BETWEEN 0 AND 100),
    match_method        TEXT NOT NULL CHECK (match_method IN ('exact_name', 'fuzzy_token_sort_ratio', 'manual')),
    manually_verified   BOOLEAN NOT NULL DEFAULT false,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at         TIMESTAMPTZ,
    reviewed_by         TEXT,

    UNIQUE (mbid, tm_attraction_id),
    -- A human can't have reviewed something with no reviewer/timestamp,
    -- and an unverified row shouldn't carry a stale reviewer identity.
    CHECK (manually_verified = false OR (reviewed_at IS NOT NULL AND reviewed_by IS NOT NULL))
);

CREATE INDEX idx_resolution_mbid ON entity_resolution_map (mbid);
CREATE INDEX idx_resolution_tm_attraction ON entity_resolution_map (tm_attraction_id);

-- ---------------------------------------------------------------------
-- artists: the canonical artist entity, one row per real-world artist,
-- created only once a match has been accepted (see README -- populating
-- this table from entity_resolution_map is an explicit Phase 2 step,
-- not part of this ingestion/resolution phase).
--
-- `mbid` and `tm_attraction_id` are both nullable (an artist could in
-- principle be known on only one side) but at least one must be set,
-- and each is UNIQUE so a given source row backs at most one canonical
-- artist -- both enforced by the CHECK and UNIQUE constraints below.
-- ---------------------------------------------------------------------
CREATE TABLE artists (
    artist_id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    canonical_name      TEXT NOT NULL,

    mbid                UUID UNIQUE REFERENCES artist_musicbrainz_source (mbid),
    tm_attraction_id    TEXT UNIQUE REFERENCES artist_ticketmaster_source (tm_attraction_id),

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CHECK (mbid IS NOT NULL OR tm_attraction_id IS NOT NULL)
);

-- ---------------------------------------------------------------------
-- events: normalized event-level data, one row per live show, populated
-- only for artists that have been resolved to a canonical artist_id.
-- `source_tm_event_id` traces each normalized row back to the raw event
-- it came from; UNIQUE means a given raw TM event feeds at most one
-- normalized row, which is what makes a future "populate events" step
-- idempotent. It is ON DELETE SET NULL rather than CASCADE: if the raw
-- source row is later purged, the normalized historical event record
-- (which is the analytical asset of this whole project) should survive.
-- ---------------------------------------------------------------------
CREATE TABLE events (
    event_id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    artist_id           INTEGER NOT NULL REFERENCES artists (artist_id) ON DELETE CASCADE,
    source_tm_event_id  TEXT UNIQUE REFERENCES ticketmaster_source_events (tm_event_id) ON DELETE SET NULL,

    venue_name          TEXT,
    venue_city          TEXT,
    venue_country       TEXT,
    event_date          DATE,

    price_min           NUMERIC(10, 2) CHECK (price_min >= 0),
    price_max           NUMERIC(10, 2) CHECK (price_max >= 0),

    -- Single canonical genre for this event, e.g. the artist's primary
    -- genre at the time -- not the M2M tag set from MusicBrainz. One
    -- event has one genre for this analysis; FK into the same shared
    -- `genres` lookup avoids yet another free-text genre column drifting
    -- out of sync with the other two.
    genre_id            INTEGER REFERENCES genres (genre_id),

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CHECK (price_max IS NULL OR price_min IS NULL OR price_max >= price_min)
);

CREATE INDEX idx_events_artist ON events (artist_id);
CREATE INDEX idx_events_date ON events (event_date);
