"""Command line — the only intended way to run the stack.

    python -m curator serve                # API + built SPA on :5002
    python -m curator import-plex          # pull the watched corpus out of Plex
    python -m curator import-plex --dry-run
    python -m curator import-obsidian --type movie --dry-run
    python -m curator search "cowboy bebop"
    python -m curator add plex://show/5d9c084b4eefaa001f5d9d7e --type anime
    python -m curator stats
    python -m curator top movie --limit 20
    python -m curator refit [--type movie]
    python -m curator purge-history <item_id> [--apply]
"""

from __future__ import annotations

import argparse
import sys

from . import catalog, config, db, media_types, ranking, scorer
from .sources import mediastack, plex_meta


def _fmt_secs(value):
    import datetime
    if not value:
        return '—'
    return datetime.datetime.fromtimestamp(int(value)).strftime('%Y-%m-%d')


def cmd_serve(args):
    from .api import create_app
    app = create_app()
    host = args.host or config.API_HOST
    port = args.port or config.API_PORT
    print(f'Curator on http://{host}:{port}  (public: {config.PUBLIC_ORIGIN})')
    if not config.FRONTEND_DIST.exists():
        print('  ! frontend/dist missing — API only. Build it with:')
        print('    npm --prefix frontend install && npm --prefix frontend run build')
    app.run(host=host, port=port, threaded=True, debug=args.debug,
            use_reloader=args.debug)


def cmd_import_plex(args):
    db.ensure_schema()
    if not config.PLEX_DB.exists():
        print(f'! Plex library database not found at {config.PLEX_DB}', file=sys.stderr)
        return 1
    if not config.plex_token():
        print('! No Plex token (sign in to Plex, or set CURATOR_PLEX_TOKEN)',
              file=sys.stderr)
        return 1

    state = {'stage': None}

    def progress(stage, done, total):
        if stage != state['stage']:
            state['stage'] = stage
            print()
        print(f'\r  {stage}: {done}/{total}', end='', flush=True)

    result = catalog.import_plex_watched(on_progress=progress, dry_run=args.dry_run)
    print('\n')
    print(f'  ledger rows      {result["ledger_rows"]}')
    print(f'  watched movies   {result["movies"]}')
    print(f'  watched episodes {result["episodes"]} -> {result.get("shows", 0)} shows')
    print(f'  resolved titles  {result["resolved_titles"]}'
          f'  (unresolved {result["unresolved"]})')
    print(f'  by type          ' + ', '.join(
        f'{k}={v}' for k, v in sorted(result['by_type'].items())) or '—')
    if args.dry_run:
        print('\n  DRY RUN — nothing written. Sample:')
        for sample in result['samples']:
            print(f'    {sample["media_type"]:<12} {sample["title"]} ({sample["year"]})'
                  + (f'  [{sample["episodes_watched"]} eps]'
                     if sample['episodes_watched'] else ''))
    else:
        print(f'\n  created {result["created"]}  updated {result["updated"]}  '
              f'unchanged {result["unchanged"]}')
    return 0


def cmd_import_obsidian(args):
    db.ensure_schema()
    from .sources import obsidian
    try:
        overrides = obsidian.read_overrides(args.overrides)
        entries, read = obsidian.read_vault(path=args.path, media_type=args.type,
                                            overrides=overrides)
    except FileNotFoundError as exc:
        print(f'! {exc}', file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f'! overrides file is not valid JSON: {exc}', file=sys.stderr)
        return 1

    print(f'  notes read       {read["notes"]}')
    print(f'  not a contest    {read["unmapped_type"]}  (games, books, decks…)')
    print(f'  not finished     {read["wrong_status"]}')
    if read['filtered_out']:
        print(f'  other contests   {read["filtered_out"]}  (--type {args.type})')
    if read['merged']:
        print(f'  season notes     {read["merged"]} merged into their series '
              f'(the contest ranks the show, not one season)')
    if read['overridden']:
        print(f'  hand-resolved    {read["overridden"]}  (ops/obsidian-overrides.json)')
    for row in read['skipped']:
        print(f'    skipped: {row["title"]} — {row["why"]}')
    print(f'  to import        {len(entries)}'
          + (f'  ({read["untiered"]} with no star rating)' if read['untiered'] else ''))
    if not entries:
        return 0
    if args.limit:
        entries = entries[:args.limit]
        print(f'  --limit          first {len(entries)}')

    state = {'stage': None}

    def progress(stage, done, total):
        if stage != state['stage']:
            state['stage'] = stage
            print()
        print(f'\r  {stage}: {done}/{total}', end='', flush=True)

    result = catalog.import_obsidian(entries, dry_run=args.dry_run,
                                     on_progress=progress)
    print('\n')
    print(f'  resolved on Plex {result["resolved"]}/{len(entries)}')
    if result['manual']:
        print(f'\n  {len(result["manual"])} built from the overrides file — Plex has no '
              f'record of these, so they carry no poster or ids:')
        for row in result['manual']:
            print(f'    {row}')
    if result['reclassified']:
        print(f'\n  {len(result["reclassified"])} classify differently on Plex — '
              f'kept the vault\'s bucket, re-file from the library if you disagree:')
        for row in result['reclassified']:
            print(f'    {row["title"]:<38} vault={row["vault"]}  plex={row["plex"]}')
    if result['unresolved']:
        print(f'\n  {len(result["unresolved"])} NOT imported — nothing pinned down. '
              f'Add these from the Add page; the candidates Plex offered are:')
        for row in result['unresolved']:
            print(f'    {row["title"]:<40} [{row["why"]}] watched {row["watched"]}')
            for candidate in row['candidates']:
                print(f'        {candidate}')

    if args.dry_run:
        print('\n  DRY RUN — nothing written. Sample:')
        for sample in result['samples']:
            print(f'    tier {sample["tier"]}  [{sample["media_type"]:<11}] '
                  f'{sample["title"]} ({sample["year"]})')
    else:
        print(f'\n  created {result["created"]}  updated {result["updated"]}  '
              f'unchanged {result["unchanged"]}')
    return 0


def cmd_search(args):
    try:
        results = plex_meta.search(' '.join(args.query), limit=args.limit)
    except plex_meta.PlexAuthError as exc:
        print(f'! {exc}', file=sys.stderr)
        return 1
    for result in results:
        print(f'  {result["media_type"]:<12} {result["title"]} ({result["year"]})')
        print(f'    {result["guid"]}')
    if not results:
        print('  no results')
    return 0


def cmd_add(args):
    db.ensure_schema()
    try:
        item_id, action = catalog.add_from_plex(
            args.guid, media_type=args.type, tier=args.tier, watched=args.watched)
    except LookupError as exc:
        print(f'! {exc}', file=sys.stderr)
        return 1
    row = db.one('SELECT * FROM items WHERE id = ?', (item_id,))
    print(f'  {action}: [{row["media_type"]}] {row["title"]} ({row["year"]}) '
          f'#{item_id}')
    return 0


def cmd_stats(args):
    db.ensure_schema()
    print(f'  db: {config.DB_PATH}')
    last = db.get_state('last_plex_import')
    print(f'  last plex import: {_fmt_secs(last)}')
    print(f'  mediastack: ' + ', '.join(
        f'{k}={"up" if v else "down"}' for k, v in mediastack.available().items()))
    print()
    header = f'  {"contest":<14}{"titles":>7}{"rounds":>8}{"covered":>9}' \
             f'{"settled":>9}{"contested":>11}{"untiered":>10}'
    print(header)
    print('  ' + '-' * (len(header) - 2))
    for media_type in media_types.ORDERED:
        s = ranking.stats(media_type.key)
        print(f'  {media_type.label:<14}{s["total"]:>7}{s["rounds"]:>8}'
              f'{s["covered"]:>9}{s["settled"]:>9}'
              f'{s["contested_confident"]:>11}{s["untiered"]:>10}')
    print(f'\n  covered = at least {scorer.AUDIT_ROUNDS_TARGET} rounds (coverage, not '
          f'precision)\n  contested = score has left its filed tier by '
          f'{scorer.CONTESTED_GATE} sigma — a review queue, not a verdict')
    return 0


def cmd_top(args):
    db.ensure_schema()
    media_type = media_types.get(args.type)
    rows = db.query(
        'SELECT * FROM items WHERE media_type = ? AND archived = 0 '
        'ORDER BY elo_score DESC LIMIT ?', (media_type.key, args.limit))
    if not rows:
        print(f'  nothing in {media_type.label} yet')
        return 0
    print(f'  {media_type.icon} {media_type.label}')
    for index, row in enumerate(rows, 1):
        flag = ' *' if (row['elo_rounds'] or 0) < scorer.AUDIT_ROUNDS_TARGET else '  '
        print(f'  {index:>3}.{flag}{row["elo_score"]:>7.0f} '
              f'±{(row["elo_sigma"] or 0):>3.0f}  '
              f'{row["title"]} ({row["year"] or "—"})  '
              f'[tier {row["tier"] or "-"}, {row["elo_rounds"]} rounds]')
    print('\n  * = provisional (under the coverage target)')
    return 0


def cmd_refit(args):
    db.ensure_schema()
    if args.type:
        media_type = media_types.get(args.type)
        fit = ranking.refit_all(media_type.key)
        print(f'  {media_type.label}: '
              + (f'{fit.iterations} iters, converged={fit.converged}' if fit
                 else 'nothing to fit'))
    else:
        for key, fit in ranking.refit_everything().items():
            print(f'  {key}: ' + (f'{fit.iterations} iters, converged={fit.converged}'
                                  if fit else 'nothing to fit'))
    return 0


def cmd_purge_history(args):
    db.ensure_schema()
    summary = ranking.purge_item(args.item_id, apply=args.apply)
    if summary is None:
        print('! no such item', file=sys.stderr)
        return 1
    print(f'  {summary["title"]} [{summary["media_type"]}]')
    print(f'  {summary["rows_deleted"]} rows across {summary["rounds_deleted"]} rounds')
    print(f'  {summary["rows_kept_in_those_sets"]} rows in those rounds are between '
          f'OTHER titles and are kept')
    if not args.apply:
        print('\n  DRY RUN — pass --apply to write (backs up first; no undo button)')
    else:
        print(f'  backup: {summary["backup_path"]}')
        print(f'  now: {summary["after"]["score"]:.0f} '
              f'(seed for its tier: {summary["after"]["seed_for_tier"]})')
    return 0


def cmd_ensure_schema(args):
    db.ensure_schema()
    print(f'  schema ready at {config.DB_PATH}')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='curator', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('serve', help='run the API + SPA')
    p.add_argument('--host'); p.add_argument('--port', type=int)
    p.add_argument('--debug', action='store_true')
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser('import-plex', help='import the watched corpus from Plex')
    p.add_argument('--dry-run', action='store_true')
    p.set_defaults(func=cmd_import_plex)

    p = sub.add_parser('import-obsidian',
                       help='import the Obsidian media log (star ratings -> tiers)')
    p.add_argument('--path', help='the vault Media folder (default: the Mycelium one)')
    p.add_argument('--overrides', help='hand-resolved titles (default: '
                                       'ops/obsidian-overrides.json)')
    p.add_argument('--type', choices=sorted(media_types.TYPES),
                   help='import one contest only')
    p.add_argument('--limit', type=int, help='stop after N entries (for a trial run)')
    p.add_argument('--dry-run', action='store_true')
    p.set_defaults(func=cmd_import_obsidian)

    p = sub.add_parser('search', help='search Plex Discover')
    p.add_argument('query', nargs='+')
    p.add_argument('--limit', type=int, default=10)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser('add', help='add a title by Plex guid')
    p.add_argument('guid')
    p.add_argument('--type', choices=sorted(media_types.TYPES))
    p.add_argument('--tier', type=int, choices=[1, 2, 3, 4, 5])
    p.add_argument('--watched', action='store_true')
    p.set_defaults(func=cmd_add)

    p = sub.add_parser('stats', help='per-contest progress')
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser('top', help='the current leaderboard for one contest')
    p.add_argument('type', nargs='?', default='movie')
    p.add_argument('--limit', type=int, default=25)
    p.set_defaults(func=cmd_top)

    p = sub.add_parser('refit', help='recompute scores from the whole history')
    p.add_argument('--type', choices=sorted(media_types.TYPES))
    p.set_defaults(func=cmd_refit)

    p = sub.add_parser('purge-history',
                       help="wipe one title's comparisons (dry-run by default)")
    p.add_argument('item_id', type=int)
    p.add_argument('--apply', action='store_true')
    p.set_defaults(func=cmd_purge_history)

    p = sub.add_parser('ensure-schema', help='create/migrate the database')
    p.set_defaults(func=cmd_ensure_schema)

    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == '__main__':
    raise SystemExit(main())
