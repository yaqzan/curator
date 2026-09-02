"""Plex's metadata service — the catalog's metadata source, and its search.

Two endpoints, both authenticated with the same Plex token the box already has:

  * `metadata.provider.plex.tv/library/metadata/<ratingKey>[,<ratingKey>...]`
    resolves a `plex://` GUID to full metadata **and the external ids**
    (imdb/tmdb/tvdb), so nothing here needs a TMDB API key. Rating keys are
    comma-batchable — 631 watched GUIDs resolve in ~16 requests, not 631.
  * `discover.provider.plex.tv/library/search` is the add-anything surface.

Why Plex rather than TMDB directly: the watch ledger is already keyed by
`plex://` GUIDs, so this is the one provider that can resolve them without a
lookup table, and it hands back the TMDB/IMDB ids anyway if we ever want them.

Responses are cached to disk by rating key. Metadata for a released title does
not change, and a re-import should not re-fetch 600 records.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
import urllib.request

from .. import config, media_types

# **The provider caps a comma-batched request at 20 results and says nothing
# about it (don't raise this).** Asking for 40 returns 20 with a 200 and no
# marker — the other 20 simply are not in the response, which reads exactly like
# "Plex doesn't know those titles". That silently dropped 60 of 140 titles and
# ~260 of 523 episodes on the first real import. `fetch_many` also re-requests
# any gap individually, so a future cap change degrades to slow, not lossy.
BATCH_SIZE = 20
_TIMEOUT = 25
_UA = 'Curator/1.0 (+https://curator.yaqzan.dev)'


class PlexAuthError(RuntimeError):
    pass


def _cache_path(rating_key):
    config.ensure_dirs()
    safe = re.sub(r'[^A-Za-z0-9_-]', '_', str(rating_key))
    return config.CACHE_DIR / f'meta-{safe}.json'


def _request(url):
    token = config.plex_token()
    if not token:
        raise PlexAuthError(
            'No Plex token. Sign in to Plex Media Server on this box, or set '
            'CURATOR_PLEX_TOKEN.')
    sep = '&' if '?' in url else '?'
    req = urllib.request.Request(
        f'{url}{sep}X-Plex-Token={urllib.parse.quote(token)}',
        headers={'Accept': 'application/json', 'User-Agent': _UA})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode('utf-8'))


def rating_key_of(guid):
    """`plex://movie/5d77...` -> `5d77...`. Accepts a bare key unchanged."""
    if not guid:
        return None
    return str(guid).rstrip('/').rsplit('/', 1)[-1]


def fetch_many(rating_keys, use_cache=True, on_progress=None):
    """Resolve rating keys to raw Plex metadata dicts, batched and cached.

    Returns `{rating_key: raw}`. Keys that the provider does not know are simply
    absent — a GUID can outlive the record it pointed at, and one dead entry must
    not fail a 600-title import.
    """
    keys = [k for k in dict.fromkeys(rating_keys) if k]
    out, pending = {}, []

    for key in keys:
        path = _cache_path(key)
        if use_cache and path.exists():
            try:
                out[key] = json.loads(path.read_text(encoding='utf-8'))
                continue
            except (ValueError, OSError):
                pass
        pending.append(key)

    def absorb(data):
        """Store every record a response carried. Returns the keys it covered."""
        covered = set()
        for raw in (data.get('MediaContainer', {}) or {}).get('Metadata', []) or []:
            key = str(raw.get('ratingKey'))
            out[key] = raw
            covered.add(key)
            try:
                _cache_path(key).write_text(json.dumps(raw), encoding='utf-8')
            except OSError:
                pass
        return covered

    for start in range(0, len(pending), BATCH_SIZE):
        chunk = pending[start:start + BATCH_SIZE]
        url = (f'{config.PLEX_METADATA_BASE}/library/metadata/'
               f'{",".join(urllib.parse.quote(k) for k in chunk)}')
        try:
            covered = absorb(_request(url))
        except PlexAuthError:
            raise
        except Exception:
            covered = set()      # one bad key can 404 the whole batch

        # Anything the batch did not cover is asked for on its own. A genuinely
        # unknown GUID stays absent; a truncated response is fully recovered, so
        # a future change to the provider's cap degrades to slow, never to lossy.
        for key in chunk:
            if key in covered:
                continue
            try:
                absorb(_request(f'{config.PLEX_METADATA_BASE}/library/metadata/{key}'))
            except PlexAuthError:
                raise
            except Exception:
                continue
            time.sleep(0.02)

        if on_progress:
            on_progress(min(start + BATCH_SIZE, len(pending)), len(pending))
        time.sleep(0.05)      # be polite; this is someone else's service

    return out


def fetch(guid_or_key, use_cache=True):
    key = rating_key_of(guid_or_key)
    return fetch_many([key], use_cache=use_cache).get(key)


# ── classification ───────────────────────────────────────────────────────────

_DOC_GENRES = {'documentary'}
_ANIMATION_GENRES = {'animation', 'anime'}
_JP = {'japan', 'jp'}


def classify(raw):
    """Which contest this title belongs in.

    Plex has no "Anime" genre of its own, so anime is detected as
    **Animation + a Japanese origin country**, which is the signal its metadata
    actually carries (Cowboy Bebop: `Genre=[Animation, ...]`, `Country=[Japan]`).
    It is a heuristic on purpose — the user can override the type on any title
    from the library page, and that override is never recomputed by a re-import.
    """
    kind = (raw.get('type') or '').lower()
    genres = {(g.get('tag') or '').lower() for g in (raw.get('Genre') or [])}
    countries = {(c.get('tag') or '').lower() for c in (raw.get('Country') or [])}

    is_film = kind == 'movie'
    if genres & _DOC_GENRES:
        return media_types.DOCUMENTARY.key
    if (genres & _ANIMATION_GENRES) and (countries & _JP):
        return media_types.ANIME_MOVIE.key if is_film else media_types.ANIME.key
    return media_types.MOVIE.key if is_film else media_types.TV.key


def _tags(raw, field):
    return [t.get('tag') for t in (raw.get(field) or []) if t.get('tag')]


def _external_ids(raw):
    ids = {}
    for entry in raw.get('Guid') or []:
        value = entry.get('id') or ''
        if '://' in value:
            scheme, _, ident = value.partition('://')
            ids[scheme] = ident
    return ids


def normalize(raw):
    """Raw Plex metadata -> the columns `items` stores.

    Poster and art stay as remote URLs. They are already on Plex's CDN, they are
    stable, and mirroring a few hundred images locally would buy nothing but a
    cache to invalidate.
    """
    ids = _external_ids(raw)
    return {
        'plex_guid': raw.get('guid'),
        'plex_rating_key': str(raw.get('ratingKey') or '') or None,
        'plex_slug': raw.get('slug'),
        'tmdb_id': ids.get('tmdb'),
        'imdb_id': ids.get('imdb'),
        'tvdb_id': ids.get('tvdb'),
        'title': raw.get('title') or '(untitled)',
        'sort_title': (raw.get('titleSort') or raw.get('title') or '').lower(),
        'year': raw.get('year'),
        'summary': raw.get('summary'),
        'tagline': raw.get('tagline'),
        'studio': raw.get('studio'),
        'duration_ms': raw.get('duration'),
        'content_rating': raw.get('contentRating'),
        'genres': json.dumps(_tags(raw, 'Genre')),
        'countries': json.dumps(_tags(raw, 'Country')),
        'poster_url': raw.get('thumb'),
        'art_url': raw.get('art'),
        'critic_rating': raw.get('rating'),
        'audience_rating': raw.get('audienceRating'),
        'media_type': classify(raw),
    }


# ── search (the add-anything surface) ────────────────────────────────────────

def search(query, limit=12):
    """Plex Discover search, normalized to the same shape `normalize` returns.

    Used to add titles we watched somewhere other than Plex — the cinema, a
    friend's account, years ago — which is most of the interesting corpus.
    """
    query = (query or '').strip()
    if not query:
        return []
    url = (f'{config.PLEX_DISCOVER_BASE}/library/search'
           f'?query={urllib.parse.quote(query)}&limit={int(limit)}'
           f'&searchTypes=movies,tv&searchProviders=discover&includeMetadata=1')
    data = _request(url)

    seen, results = set(), []
    for hub in (data.get('MediaContainer', {}) or {}).get('SearchResults', []) or []:
        for hit in hub.get('SearchResult') or []:
            raw = hit.get('Metadata') or {}
            guid = raw.get('guid')
            if not guid or guid in seen or (raw.get('type') or '') not in ('movie', 'show'):
                continue
            seen.add(guid)
            results.append({
                'guid': guid,
                'rating_key': str(raw.get('ratingKey') or ''),
                'title': raw.get('title'),
                'year': raw.get('year'),
                'type': raw.get('type'),
                'media_type': classify(raw),
                'summary': raw.get('summary'),
                'poster_url': raw.get('thumb'),
                'studio': raw.get('studio'),
                'rating': raw.get('rating'),
            })
            if len(results) >= limit:
                break
        if len(results) >= limit:
            break

    return _reclassify_from_full_metadata(results)


def search_cached(query, limit=12):
    """`search`, with the result put on disk under the query.

    For the interactive Add page a cached search would be wrong — you retype a
    query precisely because you want another look. This exists for the bulk
    importers, which fire hundreds of one-shot lookups against someone else's
    service and are routinely re-run as a dry run and then for real.
    """
    query = (query or '').strip()
    if not query:
        return []
    config.ensure_dirs()
    slug = re.sub(r'[^A-Za-z0-9]+', '-', query.lower()).strip('-')[:60]
    digest = hashlib.sha1(f'{query}|{limit}'.encode('utf-8')).hexdigest()[:8]
    path = config.CACHE_DIR / f'search-{slug}-{digest}.json'
    if path.exists():
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            pass
    results = search(query, limit=limit)
    try:
        path.write_text(json.dumps(results), encoding='utf-8')
    except OSError:
        pass
    return results


def _reclassify_from_full_metadata(results):
    """Re-derive `media_type` from each hit's FULL metadata record.

    **Search results do not carry `Country`, so the anime heuristic cannot fire on
    them (don't classify off the search payload alone).** Spirited Away comes back
    from Discover as a plain `movie`; its full record has `Genre=[Animation, …,
    Anime]` and `Country=[Japan]` and classifies as `anime_movie`. Since the card
    shows the contest a title will land in, and the frontend sends that value back
    as the choice the user saw, a wrong preview files it in the wrong contest.

    One extra batched request per search (the whole page of hits fits in one), and
    it fails soft — a lookup that errors leaves the preview classification alone.
    """
    keys = [r['rating_key'] for r in results if r.get('rating_key')]
    if not keys:
        return results
    try:
        full = fetch_many(keys)
    except Exception:
        return results
    for result in results:
        raw = full.get(result['rating_key'])
        if raw:
            result['media_type'] = classify(raw)
    return results
