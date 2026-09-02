"""MediaStack (Radarr + Sonarr) — "do we actually have this on disk?"

Strictly an **enrichment**, never a source of truth, and never a hard dependency:
the *arrs are frequently not running (they were down when this was built), so
every call here fails soft and returns an empty index rather than raising. A
Curator page must render identically whether MediaStack is up or not.

What it adds: an ownership badge on the library card, so "watched but not kept"
and "kept but never watched" are both visible at a glance. Matching is by TMDB id
for movies and TVDB id for series — the same external ids Plex's metadata service
hands back, so no fuzzy title matching is involved.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request

from .. import config

_TIMEOUT = 8
# **The liveness probe gets its own short timeout, and the two halves are probed
# in parallel (don't collapse this back into a serial loop at _TIMEOUT).** Both
# *arrs are down more often than not, and on Windows a refused connect to a dead
# local port costs seconds rather than failing instantly — probing them serially
# at the full timeout made `/api/import/status` take **8.2s**, so the Add page
# rendered without its status block for the better part of ten seconds.
_PROBE_TIMEOUT = 1.5
_PROBE_TTL = 60.0

_probe_cache = {'at': 0.0, 'value': None}
_probe_lock = threading.Lock()


def _get(base, path, api_key, timeout=_TIMEOUT):
    url = f'{base.rstrip("/")}{path}'
    sep = '&' if '?' in url else '?'
    req = urllib.request.Request(f'{url}{sep}apikey={urllib.parse.quote(api_key)}',
                                 headers={'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def available(force=False):
    """Which halves of MediaStack are reachable. Cached for `_PROBE_TTL`."""
    with _probe_lock:
        fresh = (time.monotonic() - _probe_cache['at']) < _PROBE_TTL
        if not force and fresh and _probe_cache['value'] is not None:
            return dict(_probe_cache['value'])

    status = {}

    def probe(name, base, key):
        try:
            _get(base, '/api/v3/system/status', key, timeout=_PROBE_TIMEOUT)
            status[name] = True
        except Exception:
            status[name] = False

    threads = [threading.Thread(target=probe, args=args) for args in (
        ('radarr', config.RADARR_URL, config.RADARR_KEY),
        ('sonarr', config.SONARR_URL, config.SONARR_KEY))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=_PROBE_TIMEOUT + 1.0)
    status.setdefault('radarr', False)
    status.setdefault('sonarr', False)

    with _probe_lock:
        _probe_cache['at'] = time.monotonic()
        _probe_cache['value'] = dict(status)
    return status


def ownership_index():
    """`{('tmdb', '12345'): {...}, ('tvdb', '67890'): {...}}` — empty if down.

    Skips the (slow) full fetch entirely when the cheap cached probe already says
    nothing is listening.
    """
    reachable = available()
    if not any(reachable.values()):
        return {}

    index = {}
    try:
        for movie in _get(config.RADARR_URL, '/api/v3/movie', config.RADARR_KEY):
            if movie.get('tmdbId'):
                index[('tmdb', str(movie['tmdbId']))] = {
                    'owned': bool(movie.get('hasFile')),
                    'monitored': bool(movie.get('monitored')),
                    'quality': ((movie.get('movieFile') or {}).get('quality') or {})
                                .get('quality', {}).get('name'),
                    'size_bytes': (movie.get('movieFile') or {}).get('size'),
                    'app': 'radarr',
                }
    except Exception:
        pass

    try:
        for series in _get(config.SONARR_URL, '/api/v3/series', config.SONARR_KEY):
            if series.get('tvdbId'):
                stats = series.get('statistics') or {}
                index[('tvdb', str(series['tvdbId']))] = {
                    'owned': bool(stats.get('episodeFileCount')),
                    'monitored': bool(series.get('monitored')),
                    'episode_files': stats.get('episodeFileCount'),
                    'episode_count': stats.get('episodeCount'),
                    'size_bytes': stats.get('sizeOnDisk'),
                    'app': 'sonarr',
                }
    except Exception:
        pass
    return index


def annotate(items):
    """Attach an `ownership` key to serialized items. No-op when MediaStack is down."""
    index = ownership_index()
    if not index:
        return items
    for item in items:
        info = None
        if item.get('tmdb_id'):
            info = index.get(('tmdb', str(item['tmdb_id'])))
        if info is None and item.get('tvdb_id'):
            info = index.get(('tvdb', str(item['tvdb_id'])))
        item['ownership'] = info
    return items
