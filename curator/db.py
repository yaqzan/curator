"""SQLite connection + schema.

SQLite rather than the MySQL instance Archivist uses, because this catalog is
deliberately small — a few hundred titles we actually care about, not a mirror of
a metadata provider — and a single file makes the whole project portable and
backup-able by copy. WAL is on so the ranking surface can read while an import
writes.
"""

from __future__ import annotations

import sqlite3
import threading

from . import config

SCHEMA_VERSION = 1

_local = threading.local()

# `items` is the catalog: one row per thing we might rank. Deliberately NOT a
# mirror of every movie in existence — a title lands here because it was watched
# or because it was explicitly added.
#
# `media_type` is the contest discriminator (see media_types.py). It lives on the
# item AND on every match row: an item belongs to exactly one contest, so the
# column on `matches` is technically derivable — but it is what makes "every
# scoring read filters on media_type" enforceable, and it lets the same-contest
# invariant be checked in one place.
SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    media_type        TEXT    NOT NULL,
    plex_guid         TEXT    UNIQUE,
    plex_rating_key   TEXT,
    plex_slug         TEXT,
    tmdb_id           TEXT,
    imdb_id           TEXT,
    tvdb_id           TEXT,

    title             TEXT    NOT NULL,
    sort_title        TEXT,
    year              INTEGER,
    summary           TEXT,
    tagline           TEXT,
    studio            TEXT,
    duration_ms       INTEGER,
    content_rating    TEXT,
    genres            TEXT,              -- json array
    countries         TEXT,              -- json array
    poster_url        TEXT,
    art_url           TEXT,
    critic_rating     REAL,
    audience_rating   REAL,

    -- how it got here, and what we know about having seen it
    source            TEXT    NOT NULL DEFAULT 'manual',   -- plex-watch | search | manual
    watched           INTEGER NOT NULL DEFAULT 0,
    watch_count       INTEGER NOT NULL DEFAULT 0,
    episodes_watched  INTEGER NOT NULL DEFAULT 0,
    last_watched_at   INTEGER,

    -- the ranking columns. `tier` is the filed 1-5 rating and seeds the fit's
    -- prior; the other three are the fit's output and are rewritten wholesale on
    -- every refit. Never hand-edit them.
    tier              INTEGER,
    elo_score         REAL,
    elo_sigma         REAL,
    elo_rounds        INTEGER NOT NULL DEFAULT 0,

    notes             TEXT,
    archived          INTEGER NOT NULL DEFAULT 0,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_items_type       ON items(media_type, archived);
CREATE INDEX IF NOT EXISTS idx_items_score      ON items(media_type, elo_score DESC);
CREATE INDEX IF NOT EXISTS idx_items_tmdb       ON items(tmdb_id);

-- One row per ranking round. Set ids are allocated globally, not per contest, so
-- a set_id is never ambiguous about which contest it came from.
CREATE TABLE IF NOT EXISTS rank_sets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    media_type  TEXT    NOT NULL,
    size        INTEGER NOT NULL,
    created_at  INTEGER NOT NULL
);

-- A ranking round is stored as EVERY pair in the set (C(n,2) rows) sharing one
-- set_id, with is_tie set for same-tier pairs. That keeps History/undo working
-- over one uniform table AND makes the ordering exactly recoverable.
--
-- The scorer must NOT read these as independent duels — see
-- scorer.decompose_ranking. ranking.load_observations rebuilds the weak ordering
-- first.
CREATE TABLE IF NOT EXISTS matches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    media_type  TEXT    NOT NULL,
    set_id      INTEGER REFERENCES rank_sets(id) ON DELETE CASCADE,
    winner_id   INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    loser_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    is_tie      INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_matches_type   ON matches(media_type);
CREATE INDEX IF NOT EXISTS idx_matches_set    ON matches(set_id);
CREATE INDEX IF NOT EXISTS idx_matches_winner ON matches(winner_id);
CREATE INDEX IF NOT EXISTS idx_matches_loser  ON matches(loser_id);

CREATE TABLE IF NOT EXISTS sync_state (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  INTEGER
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect():
    """A per-thread connection. Flask is threaded, sqlite3 objects are not."""
    conn = getattr(_local, 'conn', None)
    if conn is None:
        config.ensure_dirs()
        conn = sqlite3.connect(str(config.DB_PATH), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA synchronous=NORMAL')
        _local.conn = conn
    return conn


def close():
    conn = getattr(_local, 'conn', None)
    if conn is not None:
        conn.close()
        _local.conn = None


def ensure_schema():
    conn = connect()
    with conn:
        conn.executescript(SCHEMA)
        conn.execute('INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)',
                     ('schema_version', str(SCHEMA_VERSION)))
    return conn


def query(sql, params=()):
    return connect().execute(sql, params).fetchall()


def one(sql, params=()):
    return connect().execute(sql, params).fetchone()


def execute(sql, params=()):
    conn = connect()
    with conn:
        return conn.execute(sql, params)


def get_state(key, default=None):
    row = one('SELECT value FROM sync_state WHERE key = ?', (key,))
    return row['value'] if row else default


def set_state(key, value):
    import time
    execute('INSERT INTO sync_state(key, value, updated_at) VALUES (?, ?, ?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value, '
            'updated_at=excluded.updated_at',
            (key, str(value), int(time.time())))
