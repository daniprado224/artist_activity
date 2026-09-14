-- Artist catalog (MusicBrainz) vs. live activity (Ticketmaster) data layer.

CREATE TABLE genres (
    genre_id    INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    genre_name  TEXT NOT NULL UNIQUE
);

CREATE TABLE artist_musicbrainz_source (
    mbid                UUID PRIMARY KEY,
    seed_name           TEXT,
    name                TEXT NOT NULL,
    disambiguation      TEXT,
    artist_type         TEXT,
    country             TEXT,

    -- life-span dates are often partial in MB's own data (year only, or
    -- year-month, no day) -- kept as separate columns instead of a single
    -- DATE so we never pad in a day/month MB never asserted
    began_active_year   SMALLINT,
    began_active_month  SMALLINT CHECK (began_active_month BETWEEN 1 AND 12),
    began_active_day    SMALLINT CHECK (began_active_day BETWEEN 1 AND 31),
    began_active_raw    TEXT,

    release_count       INTEGER NOT NULL DEFAULT 0 CHECK (release_count >= 0),

    raw_response        JSONB,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CHECK (began_active_month IS NULL OR began_active_year IS NOT NULL),
    CHECK (began_active_day IS NULL OR began_active_month IS NOT NULL)
);

CREATE INDEX idx_mb_source_name ON artist_musicbrainz_source (lower(name));
CREATE INDEX idx_mb_source_seed_name ON artist_musicbrainz_source (seed_name);

-- genre tags, many-to-many since an artist usually has several
CREATE TABLE musicbrainz_source_genres (
    mbid        UUID NOT NULL REFERENCES artist_musicbrainz_source (mbid) ON DELETE CASCADE,
    genre_id    INTEGER NOT NULL REFERENCES genres (genre_id) ON DELETE CASCADE,
    tag_count   INTEGER CHECK (tag_count >= 0),
    PRIMARY KEY (mbid, genre_id)
);

-- one row per attraction; venue/price/date live on ticketmaster_source_events instead
CREATE TABLE artist_ticketmaster_source (
    tm_attraction_id    TEXT PRIMARY KEY,
    seed_name           TEXT,
    name                TEXT NOT NULL,
    tm_segment          TEXT,
    tm_genre            TEXT,
    event_count         INTEGER NOT NULL DEFAULT 0 CHECK (event_count >= 0),

    raw_response        JSONB,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_tm_source_name ON artist_ticketmaster_source (lower(name));
CREATE INDEX idx_tm_source_seed_name ON artist_ticketmaster_source (seed_name);

CREATE TABLE ticketmaster_source_events (
    tm_event_id         TEXT PRIMARY KEY,
    tm_attraction_id    TEXT NOT NULL REFERENCES artist_ticketmaster_source (tm_attraction_id) ON DELETE CASCADE,

    event_name          TEXT,
    venue_name          TEXT,
    venue_city          TEXT,
    venue_country       TEXT,

    event_date          DATE,
    event_time          TIME,
    is_date_tba         BOOLEAN NOT NULL DEFAULT false,

    price_min           NUMERIC(10, 2) CHECK (price_min >= 0),
    price_max           NUMERIC(10, 2) CHECK (price_max >= 0),
    currency            CHAR(3),

    raw_response        JSONB,
    ingested_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    CHECK (price_max IS NULL OR price_min IS NULL OR price_max >= price_min)
);

CREATE INDEX idx_tm_events_attraction ON ticketmaster_source_events (tm_attraction_id);
CREATE INDEX idx_tm_events_date ON ticketmaster_source_events (event_date);

-- every candidate match considered, not just accepted ones -- an audit trail
CREATE TABLE entity_resolution_map (
    resolution_id       INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    mbid                UUID NOT NULL REFERENCES artist_musicbrainz_source (mbid) ON DELETE CASCADE,
    tm_attraction_id    TEXT NOT NULL REFERENCES artist_ticketmaster_source (tm_attraction_id) ON DELETE CASCADE,

    match_confidence    NUMERIC(5, 2) NOT NULL CHECK (match_confidence BETWEEN 0 AND 100),
    match_method        TEXT NOT NULL CHECK (match_method IN ('exact_name', 'fuzzy_token_sort_ratio', 'manual')),
    manually_verified   BOOLEAN NOT NULL DEFAULT false,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at         TIMESTAMPTZ,
    reviewed_by         TEXT,

    UNIQUE (mbid, tm_attraction_id),
    CHECK (manually_verified = false OR (reviewed_at IS NOT NULL AND reviewed_by IS NOT NULL))
);

CREATE INDEX idx_resolution_mbid ON entity_resolution_map (mbid);
CREATE INDEX idx_resolution_tm_attraction ON entity_resolution_map (tm_attraction_id);

CREATE TABLE artists (
    artist_id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    canonical_name      TEXT NOT NULL,

    mbid                UUID UNIQUE REFERENCES artist_musicbrainz_source (mbid),
    tm_attraction_id    TEXT UNIQUE REFERENCES artist_ticketmaster_source (tm_attraction_id),

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CHECK (mbid IS NOT NULL OR tm_attraction_id IS NOT NULL)
);

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
    genre_id            INTEGER REFERENCES genres (genre_id),

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CHECK (price_max IS NULL OR price_min IS NULL OR price_max >= price_min)
);

CREATE INDEX idx_events_artist ON events (artist_id);
CREATE INDEX idx_events_date ON events (event_date);
