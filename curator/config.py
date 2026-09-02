"""Paths, ports and credential lookup.

No secrets are written down here. The Plex token is read from the Windows
registry (where Plex Media Server itself keeps it) with an env override, so this
file is safe to commit and works on any box where Plex is signed in.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ── serving ──────────────────────────────────────────────────────────────────
# One port serves both /api and the built SPA, so the tunnel needs exactly one
# hostname (the Scribe pattern). 5002 is the next free slot after trader-api
# (5000) and fantasy-api (5001).
API_HOST = os.environ.get('CURATOR_HOST', '127.0.0.1')
API_PORT = int(os.environ.get('CURATOR_PORT', '5002'))
PUBLIC_ORIGIN = os.environ.get('CURATOR_PUBLIC_ORIGIN', 'https://curator.yaqzan.dev')

# ── storage ──────────────────────────────────────────────────────────────────
DATA_DIR = Path(os.environ.get('CURATOR_DATA_DIR', ROOT / 'data'))
DB_PATH = Path(os.environ.get('CURATOR_DB', DATA_DIR / 'curator.db'))
CACHE_DIR = DATA_DIR / 'cache'
POSTER_CACHE = CACHE_DIR / 'posters'
FRONTEND_DIST = ROOT / 'frontend' / 'dist'

# ── Plex ─────────────────────────────────────────────────────────────────────
# **Every local service is addressed as 127.0.0.1, never `localhost` (don't
# change these back).** On Windows `localhost` resolves to `::1` first, and these
# services bind IPv4 only — so a connection to a *dead* one burns the full
# timeout on IPv6 and then the full timeout again on IPv4, doubling every probe.
# It measured as 3.0s per dead service via `localhost` against 1.5s via
# 127.0.0.1, which is the whole reason the MediaStack liveness check felt broken.
PLEX_SERVER = os.environ.get('CURATOR_PLEX_SERVER', 'http://127.0.0.1:32400')
PLEX_METADATA_BASE = 'https://metadata.provider.plex.tv'
PLEX_DISCOVER_BASE = 'https://discover.provider.plex.tv'

# Plex's own library database. Its `metadata_item_settings` table is the watch
# ledger — and it is keyed by the global `plex://` GUID, so it survives a library
# being removed or a machine migration. That is why the importer reads it
# directly instead of asking the HTTP API for a library that may not exist any
# more (this box's does not: 0 sections, 631 watch rows).
PLEX_DB = Path(os.environ.get(
    'CURATOR_PLEX_DB',
    Path(os.environ.get('LOCALAPPDATA', '')) / 'Plex Media Server' /
    'Plug-in Support' / 'Databases' / 'com.plexapp.plugins.library.db'))

# ── MediaStack (optional enrichment; absent when the *arrs aren't running) ────
RADARR_URL = os.environ.get('CURATOR_RADARR_URL', 'http://127.0.0.1:7878')
SONARR_URL = os.environ.get('CURATOR_SONARR_URL', 'http://127.0.0.1:8989')
RADARR_KEY = os.environ.get('CURATOR_RADARR_KEY', 'bac465e61baf4d48a161a56ff39cd131')
SONARR_KEY = os.environ.get('CURATOR_SONARR_KEY', '4aabe534dfe4457797ce92f95c7d56a0')

_PLEX_TOKEN_CACHE = None


def plex_token():
    """The Plex auth token, from env or the registry Plex itself writes it to.

    Cached for the process lifetime — the registry read spawns PowerShell and the
    token does not change while the server is up.
    """
    global _PLEX_TOKEN_CACHE
    if _PLEX_TOKEN_CACHE is not None:
        return _PLEX_TOKEN_CACHE

    token = os.environ.get('CURATOR_PLEX_TOKEN') or os.environ.get('PLEX_TOKEN')
    if not token:
        try:
            out = subprocess.run(
                ['powershell', '-NoProfile', '-Command',
                 "(Get-ItemProperty 'HKCU:\\Software\\Plex, Inc.\\Plex Media Server' "
                 "-Name PlexOnlineToken -ErrorAction SilentlyContinue).PlexOnlineToken"],
                capture_output=True, text=True, timeout=20)
            token = (out.stdout or '').strip()
        except Exception:
            token = ''
    _PLEX_TOKEN_CACHE = token or ''
    return _PLEX_TOKEN_CACHE


def ensure_dirs():
    for path in (DATA_DIR, CACHE_DIR, POSTER_CACHE):
        path.mkdir(parents=True, exist_ok=True)
