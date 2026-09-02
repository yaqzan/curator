"""The catalog: adding titles, and keeping an import from trampling your edits.

The governing rule is that **an import may fill blanks but must never overwrite a
human judgement.** A re-import refreshes metadata (summaries, posters, ratings
drift) and watch counts, but `tier`, `media_type` once you have moved it, `notes`
and `archived` are yours. Getting this wrong is how a catalog stops being trusted:
one sync silently re-files everything and you can't tell which of your decisions
survived.
"""

from __future__ import annotations

import json
import time

from . import db, media_types, ranking, scorer
from .sources import plex_meta

# Columns an import is allowed to refresh. Note what is absent: tier, media_type,
# notes, archived.
_REFRESHABLE = (
    'plex_rating_key', 'plex_slug', 'tmdb_id', 'imdb_id', 'tvdb_id',
    'title', 'sort_title', 'year', 'summary', 'tagline', 'studio',
    'duration_ms', 'content_rating', 'genres', 'countries',
    'poster_url', 'art_url', 'critic_rating', 'audience_rating',
)


def tier_from_plex_rating(rating):
    """Plex stores the user's star rating 0-10. Map it onto our 1-5 ladder.

    Only ever used to seed a tier that is unset — a filed tier is never
    recomputed from anything.
    """
    if rating is None:
        return None
    try:
        value = float(rating)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return max(1, min(5, int((value + 1) // 2)))


def find_existing(fields):
    """Match by plex GUID first, then by external id, then by title+year.

    The fallbacks matter because the same film can arrive from an import and from
    a search under different Plex GUIDs (regional entries, re-releases), and two
    rows for one film would split its comparison history in half.
    """
    if fields.get('plex_guid'):
        row = db.one('SELECT * FROM items WHERE plex_guid = ?', (fields['plex_guid'],))
        if row:
            return row
    for column in ('tmdb_id', 'imdb_id', 'tvdb_id'):
        value = fields.get(column)
        if value:
            row = db.one(f'SELECT * FROM items WHERE {column} = ?', (str(value),))
            if row:
                return row
    if fields.get('title') and fields.get('year'):
        row = db.one(
            'SELECT * FROM items WHERE lower(title) = lower(?) AND year = ?',
            (fields['title'], fields['year']))
        if row:
            return row
    return None


def upsert(fields, *, source='manual', watch=None):
    """Insert or refresh one title. Returns `(item_id, 'created'|'updated')`.

    `watch` is an optional
    `{watch_count, episodes_watched, last_watched_at, user_rating}` bundle from
    the Plex ledger. Watch counts are taken as the max of what we hold and what
    arrived, never reset downward — an import from a machine that lost its
    library should not erase the fact that you saw something.
    """
    now = int(time.time())
    existing = find_existing(fields)

    if existing is None:
        row = {c: fields.get(c) for c in _REFRESHABLE}
        row['plex_guid'] = fields.get('plex_guid')
        row['media_type'] = fields.get('media_type') or media_types.DEFAULT.key
        row['source'] = source
        row['created_at'] = row['updated_at'] = now
        row['watched'] = 0
        row['watch_count'] = 0
        row['episodes_watched'] = 0
        row['last_watched_at'] = None
        row['tier'] = fields.get('tier')

        if watch:
            row['watched'] = 1 if (watch.get('watch_count') or
                                   watch.get('episodes_watched')) else 0
            row['watch_count'] = int(watch.get('watch_count') or 0)
            row['episodes_watched'] = int(watch.get('episodes_watched') or 0)
            row['last_watched_at'] = watch.get('last_watched_at')
            row['tier'] = row['tier'] or tier_from_plex_rating(watch.get('user_rating'))

        # Seed the fit's starting point so a brand-new title has a sane score
        # before anyone has compared it. The refit would do this anyway; doing it
        # here means the library list is never briefly full of nulls.
        row['elo_score'] = scorer.seed_elo(row['tier'])
        row['elo_sigma'] = scorer.PRIOR_SD * scorer.ELO_PER_LOGIT
        row['elo_rounds'] = 0

        columns = ', '.join(row)
        marks = ', '.join('?' * len(row))
        cur = db.execute(f'INSERT INTO items ({columns}) VALUES ({marks})',
                         tuple(row.values()))
        return int(cur.lastrowid), 'created'

    updates = {c: fields[c] for c in _REFRESHABLE
               if fields.get(c) not in (None, '')}
    if watch:
        updates['watch_count'] = max(int(existing['watch_count'] or 0),
                                     int(watch.get('watch_count') or 0))
        updates['episodes_watched'] = max(int(existing['episodes_watched'] or 0),
                                          int(watch.get('episodes_watched') or 0))
        if updates['watch_count'] or updates['episodes_watched']:
            updates['watched'] = 1
        last = watch.get('last_watched_at')
        if last and (existing['last_watched_at'] or 0) < last:
            updates['last_watched_at'] = last
        if existing['tier'] is None:
            seeded = tier_from_plex_rating(watch.get('user_rating'))
            if seeded:
                updates['tier'] = seeded

    # A tier the caller carries (a star rating out of the Obsidian log, `add
    # --tier`) fills a BLANK one and never replaces a filed one. Both halves of
    # that are the same rule: an untiered row is a gap, not a judgement, and the
    # judgement that is already there outranks anything an import brought.
    if existing['tier'] is None and fields.get('tier'):
        updates['tier'] = int(fields['tier'])

    if not updates:
        return int(existing['id']), 'unchanged'

    updates['updated_at'] = now
    assignments = ', '.join(f'{c} = ?' for c in updates)
    db.execute(f'UPDATE items SET {assignments} WHERE id = ?',
               (*updates.values(), existing['id']))
    return int(existing['id']), 'updated'


def add_from_plex(guid_or_key, *, media_type=None, tier=None, watched=None):
    """Add one title by Plex GUID (what the search results hand back)."""
    raw = plex_meta.fetch(guid_or_key)
    if not raw:
        raise LookupError(f'Plex has no metadata for {guid_or_key}')
    fields = plex_meta.normalize(raw)
    if media_type and media_types.is_known(media_type):
        fields['media_type'] = media_type
    if tier:
        fields['tier'] = int(tier)

    item_id, action = upsert(fields, source='search')
    if watched:
        db.execute('UPDATE items SET watched = 1, watch_count = MAX(watch_count, 1), '
                   'updated_at = ? WHERE id = ?', (int(time.time()), item_id))
    ranking.refit_all(fields['media_type'])
    return item_id, action


def import_plex_watched(on_progress=None, dry_run=False):
    """Pull the whole watched corpus out of Plex and upsert it.

    Idempotent: a second run reports every title as `unchanged` or `updated`,
    never as new, because matching goes through `find_existing`.
    """
    from .sources import plex_watch

    entries, report = plex_watch.collect_watched(on_progress=on_progress)
    result = {**report, 'created': 0, 'updated': 0, 'unchanged': 0,
              'by_type': {}, 'dry_run': bool(dry_run), 'samples': []}

    touched_types = set()
    for index, entry in enumerate(entries):
        fields = plex_meta.normalize(entry['raw'])
        result['by_type'][fields['media_type']] = \
            result['by_type'].get(fields['media_type'], 0) + 1
        if len(result['samples']) < 12:
            result['samples'].append({
                'title': fields['title'], 'year': fields['year'],
                'media_type': fields['media_type'],
                'episodes_watched': entry['episodes_watched'],
            })
        if dry_run:
            continue

        _, action = upsert(fields, source='plex-watch', watch={
            'watch_count': entry['watch_count'],
            'episodes_watched': entry['episodes_watched'],
            'last_watched_at': entry['last_watched_at'],
            'user_rating': entry['user_rating'],
        })
        result[action] += 1
        touched_types.add(fields['media_type'])
        if on_progress and index % 25 == 0:
            on_progress('storing', index + 1, len(entries))

    if not dry_run:
        for media_type in touched_types:
            ranking.refit_all(media_type)
        db.set_state('last_plex_import', int(time.time()))
    return result


# ── the Obsidian media log ───────────────────────────────────────────────────

# Which Plex `type` a contest's titles must come back as. A vault entry filed as
# a Film that resolves to a series is a bad match, not a re-classification.
#
# Documentaries are deliberately absent: that contest asks "order these
# documentaries" and does not care whether one is a film or a series, so
# Planet Earth II and Dear Zachary are both legitimate answers.
_EXPECTED_KIND = {
    media_types.MOVIE.key: 'movie',
    media_types.ANIME_MOVIE.key: 'movie',
    media_types.TV.key: 'show',
    media_types.ANIME.key: 'show',
}


def _distinct_films(hits):
    """Collapse the same film appearing more than once in one search.

    Discover's index carries duplicate records — `Fight Club (1999)` twice, and
    a year-less shadow of half of everything. Left alone they read as a genuine
    "two films answer to this name" and would refuse an import that is not
    actually ambiguous at all.
    """
    seen, out = set(), []
    for hit in hits:
        key = (str(hit.get('title') or '').lower(), hit.get('year'))
        if key in seen:
            continue
        seen.add(key)
        out.append(hit)
    dated = [h for h in out if h.get('year')]
    return dated or out


def resolve_obsidian_entry(entry, search=None):
    """Find the Plex record one vault note means. Returns `(hit, why, candidates)`.

    The note gives a title and nothing else — no id, usually no year — so a match
    is **accepted only on an exact normalized title, and only when exactly one
    film answers to it**. Never on Discover's best-effort ranking: for a title
    like `Pearl` or `The Apprentice` its top hit is not the famous one, and *a
    wrong film filed under a real star rating is worse than a gap*, because
    nothing downstream can tell that it is wrong. Everything that does not pin
    down is handed back for a human.
    >
    Two things narrow the field. A `(1982)` in the note's own name is an
    assertion and settles it outright. Failing that, the year the note says it
    was watched fences out releases that did not exist yet — which separates a
    remake from its original for free, but says nothing between an original and
    an obscure namesake that is also older, so those stay ambiguous.
    """
    from .sources import obsidian, plex_meta
    search = search or plex_meta.search_cached

    if entry.get('guid'):
        return {'guid': entry['guid'], 'title': entry['title']}, 'override-guid', []

    # `match` is the override's title when there is one, and that is the title
    # PLEX uses — so it, not the note's name, is what the compare runs against.
    query = entry.get('query') or entry['title']
    wanted = obsidian.normalize_title(entry.get('match') or entry['title'])
    kind = _EXPECTED_KIND.get(entry['media_type'])
    # 20, not 8: `Her` and `Pearl` are common enough words that the film itself
    # sits well down a page otherwise full of shows that merely contain them.
    hits = search(query, limit=20) or []

    named = [h for h in hits if obsidian.normalize_title(h.get('title')) == wanted]
    exact = _distinct_films([h for h in named if not kind or h.get('type') == kind])
    if not exact:
        if not hits:
            return None, 'no-results', []
        return None, ('wrong-kind' if named else 'no-match'), hits[:4]

    asserted = entry.get('asserted_year')
    if asserted:
        dated = [h for h in exact if h.get('year') == asserted]
        if len(dated) == 1:
            return dated[0], 'matched-year', dated
        if dated:
            exact = dated

    watched = entry.get('watch_year')
    if watched:
        # A release later than the watch is impossible. If that leaves nothing,
        # the note's own dates are wrong rather than the catalog — keep the field.
        plausible = [h for h in exact if not h.get('year') or h['year'] <= watched]
        exact = plausible or exact

    if len(exact) > 1:
        return None, 'ambiguous', exact[:6]
    return exact[0], 'matched', exact


def import_obsidian(entries, *, dry_run=False, on_progress=None):
    """Resolve vault entries against Plex and upsert the ones that pin down.

    Idempotent for the same reasons `import_plex_watched` is: everything lands
    through `upsert`/`find_existing`, so a title the Plex import already brought
    in is *updated* — picking up the star rating as its tier if it had none —
    rather than duplicated into a second row with half the history.
    """
    from .sources import plex_meta

    result = {'created': 0, 'updated': 0, 'unchanged': 0, 'resolved': 0,
              'unresolved': [], 'reclassified': [], 'manual': [],
              'dry_run': bool(dry_run), 'samples': []}
    touched_types = set()

    for index, entry in enumerate(entries):
        if on_progress:
            on_progress('resolving', index + 1, len(entries))

        # A title no provider admits exists. Still watched, still rated, still
        # belongs in its contest — built from the override with no Plex record.
        if entry.get('manual'):
            fields = {'title': entry.get('match') or entry['title'],
                      'year': entry.get('asserted_year'),
                      'media_type': entry['media_type'], 'tier': entry.get('tier'),
                      'sort_title': (entry.get('match') or entry['title']).lower()}
            result['resolved'] += 1
            result['manual'].append(f'{fields["title"]} ({fields["year"]})')
            if not dry_run:
                _, action = upsert(fields, source='obsidian-manual', watch={
                    'watch_count': 1, 'episodes_watched': 0,
                    'last_watched_at': entry.get('watched_at'), 'user_rating': None,
                })
                result[action] += 1
                touched_types.add(fields['media_type'])
            continue

        try:
            hit, why, candidates = resolve_obsidian_entry(entry)
        except plex_meta.PlexAuthError:
            raise
        except Exception as exc:                       # one dead lookup of 200
            hit, why, candidates = None, f'error: {exc}', []

        if not hit:
            result['unresolved'].append({
                'title': entry['note'], 'why': why, 'watched': entry.get('epoch'),
                'candidates': [f'{c.get("title")} ({c.get("year")})'
                               for c in candidates],
            })
            continue
        result['resolved'] += 1

        raw = plex_meta.fetch(hit['guid'])
        if not raw:
            result['unresolved'].append({'title': entry['note'], 'why': 'no-metadata',
                                         'watched': entry.get('epoch'),
                                         'candidates': []})
            continue
        fields = plex_meta.normalize(raw)

        # The vault's own bucket wins: it files anime films and documentaries
        # separately, so a note that says "Film" is asserting this is neither.
        # Plex's heuristic disagreeing is worth reporting, not obeying.
        if fields['media_type'] != entry['media_type']:
            result['reclassified'].append({
                'title': f'{fields["title"]} ({fields["year"]})',
                'vault': entry['media_type'], 'plex': fields['media_type'],
            })
        fields['media_type'] = entry['media_type']
        fields['tier'] = entry.get('tier')

        if len(result['samples']) < 12:
            result['samples'].append({
                'title': fields['title'], 'year': fields['year'],
                'media_type': fields['media_type'], 'tier': entry.get('tier'),
                'from': entry['title'],
            })
        if dry_run:
            continue

        _, action = upsert(fields, source='obsidian', watch={
            'watch_count': 1,
            'episodes_watched': 0,
            'last_watched_at': entry.get('watched_at'),
            'user_rating': None,
        })
        result[action] += 1
        touched_types.add(fields['media_type'])

    if not dry_run:
        for media_type in touched_types:
            ranking.refit_all(media_type)
        db.set_state('last_obsidian_import', int(time.time()))
    return result


# ── serialization ────────────────────────────────────────────────────────────

def serialize(row):
    """One item, in the shape the frontend consumes.

    Carries the derived ranking signals (implied tier, boundary margin, contested,
    settled) so no page has to reimplement the scorer's thresholds in TypeScript.
    """
    item = dict(row)
    for column in ('genres', 'countries'):
        try:
            item[column] = json.loads(item.get(column) or '[]')
        except (ValueError, TypeError):
            item[column] = []
    item['watched'] = bool(item.get('watched'))
    item['archived'] = bool(item.get('archived'))

    score, sigma, tier = item.get('elo_score'), item.get('elo_sigma'), item.get('tier')
    item['implied_tier'] = scorer.implied_tier(score)
    item['boundary_margin'] = scorer.boundary_margin(score, tier)
    item['contested'] = scorer.is_contested(score, sigma, tier,
                                            gate=scorer.CONTESTED_GATE)
    item['contested_raw'] = scorer.is_contested(score, sigma, tier)
    item['settled'] = scorer.is_settled(score, sigma, tier)
    item['provisional'] = (item.get('elo_rounds') or 0) < scorer.AUDIT_ROUNDS_TARGET
    item['tier_label'] = media_types.TIER_LABELS.get(tier)
    return item
