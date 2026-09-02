# CLAUDE.md

Guidance for Claude Code when working in this repository. Global single-developer
policy applies (`~/.claude/CLAUDE.md`): work on `master`, dirty worktree is normal,
commits are checkpoints.

## What this is

**Curator** ranks what we've watched. Films, TV, anime and documentaries each get
their own comparison contest; elicitation is "order these six, ties allowed";
score is a batch Bradley-Terry / Plackett-Luce MAP fit over the whole comparison
history, refit after every round. Scorer is a port of Archivist's
`archiver/local_tube/face_rank.py` — that is the version measured against a real
corpus (`C:\Development\Archivist\.claude\docs\face-rank-elo.md` is the long
form); only the domain changed.

Live at **https://curator.yaqzan.dev** (own cloudflared tunnel -> `127.0.0.1:5002`),
kept up by the "Curator Watchdog" scheduled task (every 5 min -> `server.ps1
start` for whatever's down). Also linked from yaqzan.dev/projects.

## Commands

```powershell
# API + built SPA on :5002 (the only intended way to run it)
python -m curator serve

# Pull the watched corpus out of Plex. Idempotent.
python -m curator import-plex [--dry-run]

# Pull the Obsidian media log in. Its star ratings become tiers. Idempotent.
python -m curator import-obsidian [--type movie] [--dry-run] [--overrides FILE]

# Add anything else
python -m curator search "cowboy bebop"
python -m curator add plex://show/5d9c084b4eefaa001f5d9d7e --type anime --tier 5

# Per-contest progress / standings
python -m curator stats
python -m curator top anime --limit 20

# Recompute every score from the whole history (after a bulk tier edit)
python -m curator refit [--type movie]

# Wipe ONE title's comparisons — for rounds judged on wrong information.
# Dry-run by default; --apply backs the rows up first (no undo button).
python -m curator purge-history <item_id> [--apply]

pytest                      # 63 tests, no DB or network needed
```

Frontend:

```powershell
npm --prefix frontend install
npm --prefix frontend run build     # tsc --noEmit && vite build -> frontend/dist
npm --prefix frontend run dev       # :5180, proxies /api to :5002
```

Service management (from `C:\Development\server.ps1`):

```powershell
.\server.ps1 -Action start   -Service curator      # api + tunnel
.\server.ps1 -Action status  -Service curator
.\server.ps1 -Action logs    -Service curator-api
```

## Architecture

```
Plex library DB (watch ledger) ─┐
Obsidian media log (tiers) ─────┤
Plex metadata provider ─────────┼→ catalog.py → items (SQLite)
Plex Discover search ───────────┘                  │
                                                   ├→ ranking.py ─┐
Radarr/Sonarr (optional badge) ────────────────────┘              │
                                                        scorer.py ─┴→ elo_score / elo_sigma
```

| Module | Owns |
|---|---|
| `media_types.py` | **The contest registry.** The only place a type key, label or piece of surface copy is written down. |
| `scorer.py` | Pure maths. The batch fit, the ranking decomposition, set selection, the tier/boundary arithmetic. Knows nothing about media. |
| `ranking.py` | **The one definition** of "read the history and refit everyone", plus recording, undo, correction, purge and the audit pool. |
| `catalog.py` | Adding and refreshing titles, and the rule that an import never overwrites a human judgement. |
| `db.py` | SQLite schema + thread-local connections. |
| `api.py` | Flask: `/api/*` plus the built SPA on one port. |
| `sources/plex_watch.py` | The watch ledger, read out of Plex's own SQLite. |
| `sources/plex_meta.py` | Metadata resolution, classification and search. |
| `sources/obsidian.py` | The vault's media log — the only source of *tiers*. |
| `sources/mediastack.py` | Optional "on disk" badge from Radarr/Sonarr. Fails soft. |

Frontend is Vite + React + TypeScript in `frontend/`, served from `frontend/dist`
by Flask in production.

## Load-bearing decisions (don't silently revert)

Full detail: [`ranking-scoring-design.md`](.claude/docs/ranking-scoring-design.md).

- **Every media type is a separate contest, every scoring read filters on it** —
  same 800-1200 scale across contests means a missing filter fails *silently*.
  `ranking.record_ranking` rejects a mixed-type round; re-filing a title drops its
  history.
- **Scorer is a batch fit, not online ELO** — for order independence, transitive
  propagation, and a real posterior SD, not accuracy (algorithm choice is within
  0.01 Spearman regardless).
- **A round stores every pair (C(n,2), one `set_id`) but is never scored as
  independent duels** — that inflates one judgement 1.5x-2.7x (n=4..9) and breaks
  `elo_sigma`, which the calibration gate runs on.
- **Ordering six beats picking a winner from two** — Fisher information 3.550 vs
  0.500 at equal scores (7.1x); ties are signal, not a convenience. Grouping
  applies to the clicks made, not the previously placed title.
- **Two ways to place, both must work**: click appends; drag places at a chosen
  position (middle of a row = tie, edge = rank above/below). Drop indicator is
  drawn ON the row, never a list placeholder. Drag is mouse/pen only — touch stays
  on tap-to-place.
- **Submission is an event consequence, never a `useEffect`** — a synchronous
  `submitLock` ref (StrictMode-safe) guards `commit()`; an effect-driven version
  double-recorded every round.
- **`PRIOR_SD = 0.45`** — don't tighten it (detection of a mis-tiered title over 4
  rounds: 19% at 0.30, 51% at 0.45, 53% at 0.60).
- **Three progress signals, don't collapse them**: `covered` (>=4 rounds,
  coverage only), `contested`/`contested_confident` (the actual finding),
  `settled` (never drive a progress bar with this — unreachable for boundary-far
  titles).
- **`scorer.select_set` deliberately crosses tier boundaries** (`boundary_bias`
  0.65, fenced +/-1 tier) — nearest-score sorting alone made 86.8% of matches
  intra-tier. `audit_pool` must pass `priority` or thin-corpus sigma lets
  already-audited titles dominate.
- **Import fills blanks, never overwrites a judgement** — `catalog._REFRESHABLE`
  excludes `tier`/`media_type`/`notes`/`archived`; the one exception is setting a
  NULL tier. Matching: GUID -> external id -> title+year.

## Data sources — why these

Full detail: [`data-sources.md`](.claude/docs/data-sources.md).

- **Watch ledger from Plex's SQLite, not HTTP** — `metadata_item_settings` keys on
  the global `plex://` GUID, survives migration. **Copy the WAL** (`-wal`/`-shm`),
  not just `.db` — the main file alone reports 0 rows. First import: 107 films +
  523 episodes -> 41 shows = 148 titles.
- **Tiers from the Obsidian media log**
  (`C:\Obsidian\Mycelium\Recommendations\Reading List\Media`), not Plex — 802
  notes, seeded 367/388 tiers vs Plex's 6/148. TV/anime logged per season;
  `obsidian.series_title` groups them, tier = mean rounded up at the half. Title
  resolved against Discover only on an exact unambiguous normalized match (never
  top-hit) — a wrong match is worse than a gap. Unresolvable entries go in
  `ops/obsidian-overrides.json` (153 of 460 needed one).
- **Metadata from Plex's metadata provider** — no TMDB key needed. Batch endpoint
  silently caps at 20 (`BATCH_SIZE = 20`, `fetch_many` re-requests gaps).
- **Classification is Animation + Japan**, a heuristic, re-classified from full
  metadata (not the search payload, which carries no `Country`).
- **MediaStack is enrichment, never a dependency** — fails soft, cached 60s. Use
  `127.0.0.1` not `localhost` (global rule; measured 3.0s vs 1.5s per dead
  service here).

## Ops

- **Tunnel:** own `curator` tunnel (`9ff21c24-4350-4f4f-b20b-e966e0c6d819`),
  config at `ops/cloudflared-config.yml`, `curator.yaqzan.dev` ->
  `http://127.0.0.1:5002`. Own tunnel rather than an ingress rule on
  `trading-api` (dashboard-managed, cannot be extended from config) — same
  pattern as Scribe. Global rule applies: pass `--config` + tunnel UUID, not the
  name, with `--overwrite-dns` to fix a bad record. ~30s of edge 502s after a
  connector restart is normal.
- **`server.ps1`** knows `curator`, `curator-api`, `curator-tunnel`; process
  matcher keys on `-m curator` **plus `serve`**, so `import-plex`/`stats`/`top`
  runs aren't mistaken for a stale server.
- **Storage:** `data/curator.db` (SQLite, WAL) — backup by copying the file.
  Metadata cache under `data/cache/`; purge backups under `data/purges/`.
- **Secrets:** none in the repo. Plex token read from the registry
  (`HKCU:\Software\Plex, Inc.\Plex Media Server`), overridable with
  `CURATOR_PLEX_TOKEN`.
