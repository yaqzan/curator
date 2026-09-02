# Curator

Ranks what I've watched. Films, TV, anime, and documentaries each get their own
comparison contest, ordered six titles at a time instead of rated with stars.

Live: https://curator.yaqzan.dev

## Why I built it

Star ratings drift. I'd give three films an 8 over a few years and lose track of which
one I actually preferred more, and recalibrating a 1-10 scale in my head is harder than
just answering "which of these did I like better." Curator only ever asks one thing:
order six titles, ties allowed, and it works your rating out from your answers.

## How it works

- Titles come from Plex's own watch ledger, read directly out of its SQLite database
  rather than the HTTP API, because the ledger is keyed by a global GUID and survives
  the library being removed. This machine's Plex reports zero library sections after a
  migration, but all 107 watch rows are still intact.
- Every media type is a separate contest sharing one 800-1200 scoring scale via
  `media_types.py`, the single place a contest's label or key is written down.
- `scorer.py` is a batch Bradley-Terry and Plackett-Luce MAP fit run over the whole
  comparison history and refit after every round, giving order-independence and a real
  posterior uncertainty per title instead of an online Elo that drifts with judging
  order.
- `catalog.py` enforces that a re-import from Plex or my Obsidian vault fills in blanks
  but never overwrites a tier I set by hand.
- Radarr and Sonarr, if running, add an "on disk" badge to library cards. If they
  aren't, nothing about the app changes.

`.claude/` holds the guidance I give Claude Code when it works in this repo. I develop
with agents heavily, and the docs there are the project's memory.

## What I think is interesting

- The core interaction has a real information-theoretic argument behind it, not just a
  UX preference: ordering six items carries 7.1 times the Fisher information of a single
  head-to-head duel (3.55 versus 0.50), meaning roughly 110 rounds reach the same
  accuracy as 700 pairwise comparisons.
- The watch history is read straight from Plex's SQLite ledger instead of its API,
  specifically because the ledger survives a library being deleted or moved. All 107
  watch rows on this machine outlived a full library migration that zeroed out every
  Plex section (`curator/sources/plex_watch.py`).
- `scorer.py` is an explicit, documented port of a face-ranking scorer I built for a
  different project, carrying forward an already-validated batch-fit design instead of
  re-deriving the math from scratch.
- Importing never overwrites a judgement. A re-import from Plex or Obsidian fills blank
  fields but a hand-set tier always wins, mirroring the same rule I use in a much larger
  ingestion pipeline elsewhere (`curator/catalog.py`).
- `curator/config.py` opens with a comment stating it holds no secrets, then reads the
  Plex token from an environment variable or the Windows registry key Plex itself
  writes. Zero secrets on disk by design, not by omission.

## Running it

This is a personal self-hosted tool tied to my Plex library, my Obsidian vault, and my
Windows task scheduler. Here's what you'd need to run it yourself:

```powershell
pip install -r requirements.txt
npm --prefix frontend install && npm --prefix frontend run build

python -m curator import-plex     # pull the watched corpus
python -m curator serve           # http://127.0.0.1:5002
pytest                            # 63 tests, no database or network needed
```

A Plex token is read from `CURATOR_PLEX_TOKEN` or, on Windows, the registry key Plex
itself populates. Radarr and Sonarr integration is optional and fails soft if either
isn't running.

## Layout

```
curator/
  media_types.py    the contest registry, the only place a type key is written
  scorer.py          the batch Bradley-Terry / Plackett-Luce fit
  ranking.py          history, refit, undo, purge, the audit pool
  catalog.py          adding titles; imports never overwrite a hand-set judgement
  sources/            Plex watch ledger, Plex metadata + search, Radarr/Sonarr badge
frontend/            Vite + React + TypeScript
ops/                  cloudflared tunnel config
tests/                63 tests; no database or network needed
```

## Status

Live and in daily use for my own watch tracking. This is a snapshot of a private working
repo; the commit history isn't published.

## License

MIT
