"""What we have actually watched, read straight out of Plex's own database.

`metadata_item_settings` is Plex's per-account watch ledger: one row per title
with `view_count`, `last_viewed_at` and the user's own star `rating`. It is keyed
by the **global `plex://` GUID**, not by a local library id — which is why it is
the right source here and the HTTP API is not:

    this box, right now:  /library/sections -> 0 sections
                          metadata_item_settings -> 631 rows, 630 with views

The libraries were lost in the machine migration; the watch history was not.
Reading the ledger directly recovers all of it.

**Episodes roll up to their show.** 523 of the 630 watched rows are episodes, and
we rank shows, not episodes — so episodes are grouped by their `grandparentGuid`
(resolved from the metadata provider) and the show gets an `episodes_watched`
count plus the most recent view date.

The database is opened **read-only on a copy**, WAL and all. Plex is a live
process holding the real file; the copy is a snapshot, and this module never
writes to Plex's data under any circumstance.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path

from .. import config
from . import plex_meta


class PlexDatabaseMissing(RuntimeError):
    pass


def _snapshot(db_path=None):
    """Copy the library DB (+ WAL + SHM) somewhere we can safely read it.

    The WAL matters: the main .db file on this box reports 0 rows on its own
    because everything recent lives in the 1.4 MB write-ahead log. Copying the
    .db alone silently reads an empty database — which looks exactly like
    "nothing has been watched".
    """
    src = Path(db_path or config.PLEX_DB)
    if not src.exists():
        raise PlexDatabaseMissing(f'Plex library database not found at {src}')

    tmp = Path(tempfile.mkdtemp(prefix='curator-plex-'))
    dest = tmp / 'library.db'
    shutil.copy2(src, dest)
    for suffix in ('-wal', '-shm'):
        side = Path(str(src) + suffix)
        if side.exists():
            shutil.copy2(side, Path(str(dest) + suffix))
    return dest


def read_watch_ledger(db_path=None, account_id=None, min_views=1):
    """Raw watch rows: `[{guid, kind, view_count, last_viewed_at, rating}, ...]`.

    `account_id=None` means the owner account (id 1), which is the only one with
    rows on this box. `kind` is Plex's own GUID segment — `movie`, `episode`,
    `show`.
    """
    snapshot = _snapshot(db_path)
    try:
        conn = sqlite3.connect(f'file:{snapshot}?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        sql = ('SELECT account_id, guid, view_count, last_viewed_at, rating '
               'FROM metadata_item_settings WHERE view_count >= ?')
        params = [int(min_views)]
        if account_id is not None:
            sql += ' AND account_id = ?'
            params.append(int(account_id))
        rows = conn.execute(sql, params).fetchall()
        conn.close()
    finally:
        try:
            shutil.rmtree(snapshot.parent, ignore_errors=True)
        except OSError:
            pass

    out = []
    for row in rows:
        guid = row['guid'] or ''
        if not guid.startswith('plex://'):
            continue
        kind = guid[len('plex://'):].split('/', 1)[0]
        out.append({
            'guid': guid,
            'kind': kind,
            'rating_key': plex_meta.rating_key_of(guid),
            'view_count': int(row['view_count'] or 0),
            'last_viewed_at': row['last_viewed_at'],
            'user_rating': row['rating'],
        })
    return out


def _newest(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def collect_watched(db_path=None, account_id=None, on_progress=None):
    """The watched corpus, ready to upsert: one entry per rankable *title*.

    Movies map one-to-one. Episodes are resolved (batched, cached) and rolled up
    to their show, so "watched 4 episodes of Severance" becomes one Severance row
    carrying `episodes_watched=4` rather than four un-rankable episode rows.

    Returns `(entries, report)`. Each entry is
    `{rating_key, raw, watch_count, episodes_watched, last_watched_at, user_rating}`.
    """
    ledger = read_watch_ledger(db_path, account_id)
    movies = [r for r in ledger if r['kind'] == 'movie']
    shows = [r for r in ledger if r['kind'] == 'show']
    episodes = [r for r in ledger if r['kind'] == 'episode']

    report = {
        'ledger_rows': len(ledger),
        'movies': len(movies),
        'episodes': len(episodes),
        'shows_direct': len(shows),
        'unresolved': 0,
    }

    def progress(stage, done, total):
        if on_progress:
            on_progress(stage, done, total)

    # Episodes -> their show. This is the only part that needs the network.
    progress('resolving episodes', 0, len(episodes))
    ep_meta = plex_meta.fetch_many(
        [r['rating_key'] for r in episodes],
        on_progress=lambda d, t: progress('resolving episodes', d, t))

    rollup = {}     # show rating_key -> aggregate
    for row in episodes:
        raw = ep_meta.get(row['rating_key'])
        show_guid = (raw or {}).get('grandparentGuid')
        if not show_guid:
            report['unresolved'] += 1
            continue
        key = plex_meta.rating_key_of(show_guid)
        agg = rollup.setdefault(key, {'episodes_watched': 0, 'last_watched_at': None,
                                      'watch_count': 0, 'user_rating': None})
        agg['episodes_watched'] += 1
        agg['watch_count'] += row['view_count']
        agg['last_watched_at'] = _newest(agg['last_watched_at'], row['last_viewed_at'])

    # A show may also carry its own ledger row (marked watched wholesale).
    for row in shows:
        agg = rollup.setdefault(row['rating_key'], {
            'episodes_watched': 0, 'last_watched_at': None,
            'watch_count': 0, 'user_rating': None})
        agg['watch_count'] += row['view_count']
        agg['last_watched_at'] = _newest(agg['last_watched_at'], row['last_viewed_at'])
        agg['user_rating'] = agg['user_rating'] or row['user_rating']

    report['shows'] = len(rollup)

    # Now resolve the titles themselves (movies + rolled-up shows).
    title_keys = [r['rating_key'] for r in movies] + list(rollup)
    progress('resolving titles', 0, len(title_keys))
    title_meta = plex_meta.fetch_many(
        title_keys, on_progress=lambda d, t: progress('resolving titles', d, t))

    entries = []
    for row in movies:
        raw = title_meta.get(row['rating_key'])
        if not raw:
            report['unresolved'] += 1
            continue
        entries.append({
            'rating_key': row['rating_key'],
            'raw': raw,
            'watch_count': row['view_count'],
            'episodes_watched': 0,
            'last_watched_at': row['last_viewed_at'],
            'user_rating': row['user_rating'],
        })

    for key, agg in rollup.items():
        raw = title_meta.get(key)
        if not raw:
            report['unresolved'] += 1
            continue
        entries.append({
            'rating_key': key,
            'raw': raw,
            'watch_count': agg['watch_count'],
            'episodes_watched': agg['episodes_watched'],
            'last_watched_at': agg['last_watched_at'],
            'user_rating': agg['user_rating'],
        })

    report['resolved_titles'] = len(entries)
    return entries, report
