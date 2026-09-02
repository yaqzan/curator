"""Flask API + static host for the built SPA.

One port serves both, so the tunnel needs exactly one hostname
(`curator.yaqzan.dev`). Everything the frontend needs is under `/api`; anything
else falls through to the SPA's `index.html` so client-side routes deep-link.
"""

from __future__ import annotations

import json
import queue
import threading
import time

from flask import Flask, Response, jsonify, request, send_from_directory

from . import catalog, config, db, media_types, ranking, scorer
from .sources import mediastack, plex_meta

DEFAULT_SET_SIZE = 6
ALLOWED_SET_SIZES = (4, 6, 9)


def create_app():
    db.ensure_schema()
    app = Flask(__name__, static_folder=None)

    # ── health / registry ────────────────────────────────────────────────────

    @app.get('/api/health')
    def health():
        try:
            total = db.one('SELECT COUNT(*) c FROM items')['c']
            rounds = db.one('SELECT COUNT(*) c FROM rank_sets')['c']
            ok = True
        except Exception:
            total = rounds = 0
            ok = False
        return jsonify({'status': 'ok' if ok else 'error', 'items': total,
                        'rounds': rounds, 'version': '1.0.0'})

    @app.get('/api/types')
    def types():
        """The contest registry plus per-contest progress.

        The frontend mirrors this for rendering copy — it is not a second source
        of truth, and no type key is written down in TypeScript.
        """
        out = []
        for media_type in media_types.ORDERED:
            entry = media_type._asdict()
            entry['stats'] = ranking.stats(media_type.key)
            out.append(entry)
        return jsonify({'types': out, 'tier_labels': media_types.TIER_LABELS,
                        'set_sizes': ALLOWED_SET_SIZES,
                        'default_set_size': DEFAULT_SET_SIZE})

    # ── library ──────────────────────────────────────────────────────────────

    @app.get('/api/items')
    def list_items():
        media_type = request.args.get('type')
        search = (request.args.get('q') or '').strip()
        sort = request.args.get('sort') or 'score'
        include_archived = request.args.get('archived') == '1'
        watched = request.args.get('watched')
        tier = request.args.get('tier')
        limit = min(int(request.args.get('limit') or 500), 2000)
        offset = int(request.args.get('offset') or 0)

        where, params = [], []
        if media_type and media_types.is_known(media_type):
            where.append('media_type = ?')
            params.append(media_types.get(media_type).key)
        if not include_archived:
            where.append('archived = 0')
        if watched in ('0', '1'):
            where.append('watched = ?')
            params.append(int(watched))
        if tier == 'none':
            where.append('tier IS NULL')
        elif tier:
            where.append('tier = ?')
            params.append(int(tier))
        if search:
            where.append('(title LIKE ? OR studio LIKE ?)')
            params.extend([f'%{search}%'] * 2)

        order = {
            'score': 'elo_score DESC, title ASC',
            'title': 'sort_title ASC',
            'year': 'year DESC, title ASC',
            'recent': 'last_watched_at DESC, created_at DESC',
            'added': 'created_at DESC',
            'rounds': 'elo_rounds ASC, elo_sigma DESC',
            'uncertainty': 'elo_sigma DESC',
        }.get(sort, 'elo_score DESC, title ASC')

        clause = ('WHERE ' + ' AND '.join(where)) if where else ''
        rows = db.query(
            f'SELECT * FROM items {clause} ORDER BY {order} LIMIT ? OFFSET ?',
            (*params, limit, offset))
        total = db.one(f'SELECT COUNT(*) c FROM items {clause}', tuple(params))['c']

        items = [catalog.serialize(r) for r in rows]
        if request.args.get('ownership') == '1':
            items = mediastack.annotate(items)
        return jsonify({'items': items, 'total': int(total),
                        'limit': limit, 'offset': offset})

    @app.get('/api/items/<int:item_id>')
    def get_item(item_id):
        row = db.one('SELECT * FROM items WHERE id = ?', (item_id,))
        if not row:
            return jsonify({'error': 'not found'}), 404
        item = catalog.serialize(row)

        history = db.query(
            'SELECT m.id, m.set_id, m.is_tie, m.created_at, m.winner_id, '
            '       m.loser_id, w.title AS winner_title, l.title AS loser_title '
            'FROM matches m '
            'JOIN items w ON w.id = m.winner_id JOIN items l ON l.id = m.loser_id '
            'WHERE m.winner_id = ? OR m.loser_id = ? ORDER BY m.id DESC LIMIT 100',
            (item_id, item_id))
        item['history'] = [dict(r) for r in history]
        item['seed_for_tier'] = scorer.seed_elo(row['tier'])
        return jsonify(item)

    @app.patch('/api/items/<int:item_id>')
    def update_item(item_id):
        row = db.one('SELECT * FROM items WHERE id = ?', (item_id,))
        if not row:
            return jsonify({'error': 'not found'}), 404
        body = request.get_json(silent=True) or {}

        updates = {}
        if 'tier' in body:
            tier = body['tier']
            updates['tier'] = None if tier in (None, '', 0) else max(1, min(5, int(tier)))
        if 'media_type' in body and media_types.is_known(body['media_type']):
            updates['media_type'] = media_types.get(body['media_type']).key
        if 'notes' in body:
            updates['notes'] = body['notes'] or None
        if 'archived' in body:
            updates['archived'] = 1 if body['archived'] else 0
        if 'watched' in body:
            updates['watched'] = 1 if body['watched'] else 0
        if not updates:
            return jsonify(catalog.serialize(row))

        updates['updated_at'] = int(time.time())
        assignments = ', '.join(f'{c} = ?' for c in updates)
        db.execute(f'UPDATE items SET {assignments} WHERE id = ?',
                   (*updates.values(), item_id))

        # A tier change moves the fit's PRIOR, which changes what every other
        # title's results imply about it too — so the whole contest is refit, not
        # just this row. Moving a title between contests refits both.
        if 'tier' in updates or 'media_type' in updates or 'archived' in updates:
            ranking.refit_all(row['media_type'])
            if updates.get('media_type') and updates['media_type'] != row['media_type']:
                # Its old comparisons belong to the old contest and would now be
                # cross-contest judgements. Drop them rather than let them leak.
                db.execute('DELETE FROM matches WHERE (winner_id = ? OR loser_id = ?)',
                           (item_id, item_id))
                ranking.refit_all(row['media_type'])
                ranking.refit_all(updates['media_type'])

        return jsonify(catalog.serialize(
            db.one('SELECT * FROM items WHERE id = ?', (item_id,))))

    @app.delete('/api/items/<int:item_id>')
    def delete_item(item_id):
        row = db.one('SELECT media_type FROM items WHERE id = ?', (item_id,))
        if not row:
            return jsonify({'error': 'not found'}), 404
        db.execute('DELETE FROM items WHERE id = ?', (item_id,))
        ranking.refit_all(row['media_type'])
        return jsonify({'deleted': item_id})

    @app.post('/api/items/<int:item_id>/purge-history')
    def purge_history(item_id):
        apply = (request.get_json(silent=True) or {}).get('apply', False)
        summary = ranking.purge_item(item_id, apply=bool(apply))
        if summary is None:
            return jsonify({'error': 'not found'}), 404
        return jsonify(summary)

    # ── adding titles ────────────────────────────────────────────────────────

    @app.get('/api/search')
    def search():
        query = request.args.get('q') or ''
        try:
            results = plex_meta.search(query, limit=int(request.args.get('limit') or 12))
        except plex_meta.PlexAuthError as exc:
            return jsonify({'error': str(exc)}), 503
        except Exception as exc:
            return jsonify({'error': f'search failed: {exc}'}), 502

        guids = [r['guid'] for r in results]
        if guids:
            marks = ','.join('?' * len(guids))
            known = {r['plex_guid']: r['id'] for r in
                     db.query(f'SELECT id, plex_guid FROM items WHERE plex_guid IN ({marks})',
                              guids)}
            for result in results:
                result['existing_id'] = known.get(result['guid'])
        return jsonify({'results': results})

    @app.post('/api/items')
    def add_item():
        body = request.get_json(silent=True) or {}
        guid = body.get('guid') or body.get('rating_key')
        if not guid:
            return jsonify({'error': 'guid required'}), 400
        try:
            item_id, action = catalog.add_from_plex(
                guid, media_type=body.get('media_type'), tier=body.get('tier'),
                watched=body.get('watched'))
        except LookupError as exc:
            return jsonify({'error': str(exc)}), 404
        except plex_meta.PlexAuthError as exc:
            return jsonify({'error': str(exc)}), 503
        return jsonify({'action': action, 'item': catalog.serialize(
            db.one('SELECT * FROM items WHERE id = ?', (item_id,)))})

    @app.post('/api/import/plex')
    def import_plex():
        """Stream the Plex watched-history import as SSE.

        Streamed rather than answered by one long POST because resolving several
        hundred episode GUIDs takes long enough that a silent spinner reads as a
        hang. The import runs on its own thread and pushes progress through a
        queue — collecting callbacks into a list and yielding them afterwards
        would produce a progress bar that only appears once the work is over.
        """
        dry_run = request.args.get('dry_run') == '1'
        events = queue.Queue()

        def run():
            # The worker gets its own thread, so it gets its own sqlite
            # connection (db.connect is thread-local) and must close it.
            try:
                result = catalog.import_plex_watched(
                    on_progress=lambda stage, done, total: events.put(
                        {'stage': stage, 'done': done, 'total': total}),
                    dry_run=dry_run)
                events.put({'stage': 'done', 'result': result})
            except Exception as exc:
                events.put({'stage': 'error', 'error': str(exc)})
            finally:
                db.close()
                events.put(None)

        threading.Thread(target=run, daemon=True).start()

        def stream():
            yield f'data: {json.dumps({"stage": "start"})}\n\n'
            while True:
                try:
                    event = events.get(timeout=15)
                except queue.Empty:
                    yield ': keepalive\n\n'      # keep the tunnel from idling out
                    continue
                if event is None:
                    break
                yield f'data: {json.dumps(event)}\n\n'

        return Response(stream(), mimetype='text/event-stream',
                        headers={'Cache-Control': 'no-cache',
                                 'X-Accel-Buffering': 'no'})

    @app.get('/api/import/status')
    def import_status():
        last = db.get_state('last_plex_import')
        return jsonify({
            'last_plex_import': int(last) if last else None,
            'plex_db': str(config.PLEX_DB),
            'plex_db_present': config.PLEX_DB.exists(),
            'plex_token': bool(config.plex_token()),
            'mediastack': mediastack.available(),
        })

    # ── ranking ──────────────────────────────────────────────────────────────

    @app.get('/api/rank/set')
    def rank_set():
        """The next N titles to order. The elicitation surface's only input.

        Returns fewer than `size` (or none) when the contest does not have enough
        titles — the page says so rather than pretending.
        """
        media_type = media_types.get(request.args.get('type')).key
        size = int(request.args.get('size') or DEFAULT_SET_SIZE)
        if size not in ALLOWED_SET_SIZES:
            size = DEFAULT_SET_SIZE

        pool, priority = ranking.audit_pool(media_type)
        if len(pool) < 2:
            total = db.one('SELECT COUNT(*) c FROM items WHERE media_type = ? '
                           'AND archived = 0', (media_type,))['c']
            return jsonify({'type': media_type, 'items': [], 'size': size,
                            'pool': len(pool), 'total': int(total)})

        chosen = scorer.select_set(pool, size=min(size, len(pool)), priority=priority)
        ids = [c['id'] for c in chosen]
        marks = ','.join('?' * len(ids))
        rows = db.query(f'SELECT * FROM items WHERE id IN ({marks})', ids)
        by_id = {int(r['id']): catalog.serialize(r) for r in rows}
        return jsonify({
            'type': media_type,
            'question': media_types.get(media_type).question,
            'subject': media_types.get(media_type).subject,
            'size': len(ids),
            'pool': len(pool),
            'items': [by_id[i] for i in ids if i in by_id],
        })

    @app.post('/api/rank/set-result')
    def rank_set_result():
        """Record one ranking round.

        Body: `{type, tiers: [[id, id], [id], ...]}` — best first, ids sharing an
        inner list are tied.
        """
        body = request.get_json(silent=True) or {}
        media_type = media_types.get(body.get('type')).key
        tiers = body.get('tiers') or []
        try:
            result = ranking.record_ranking(media_type, tiers)
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        result['stats'] = ranking.stats(media_type)
        return jsonify(result)

    @app.post('/api/rank/undo')
    def rank_undo():
        media_type = media_types.get(
            (request.get_json(silent=True) or {}).get('type')
            or request.args.get('type')).key
        set_id = ranking.undo_last(media_type)
        if set_id is None:
            return jsonify({'error': 'nothing to undo'}), 404
        return jsonify({'undone_set': set_id, 'stats': ranking.stats(media_type)})

    @app.post('/api/matches/<int:match_id>/result')
    def correct_match(match_id):
        body = request.get_json(silent=True) or {}
        try:
            result = ranking.correct_match(match_id, winner_id=body.get('winner_id'),
                                           tie=body.get('tie'))
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        if result is None:
            return jsonify({'error': 'not found'}), 404
        return jsonify(result)

    @app.get('/api/rank/stats')
    def rank_stats():
        media_type = media_types.get(request.args.get('type')).key
        return jsonify(ranking.stats(media_type))

    @app.post('/api/rank/refit')
    def rank_refit():
        media_type = request.args.get('type')
        if media_type and media_types.is_known(media_type):
            fit = ranking.refit_all(media_types.get(media_type).key)
            return jsonify({'refit': [media_type],
                            'converged': bool(fit and fit.converged)})
        fits = ranking.refit_everything()
        return jsonify({'refit': list(fits),
                        'converged': {k: bool(v and v.converged) for k, v in fits.items()}})

    @app.get('/api/rank/review')
    def rank_review():
        """The audit's OUTPUT: titles whose score has left their filed tier.

        A review queue, not a verdict — roughly one in six flagged titles is a
        false alarm at these settings, which is a fine ratio for something a human
        looks at and a terrible one for anything automatic. `?gate=0` widens it to
        include boundary drift.
        """
        media_type = media_types.get(request.args.get('type')).key
        gate = scorer.CONTESTED_GATE if request.args.get('gate') != '0' else 0.0

        rows = db.query('SELECT * FROM items WHERE media_type = ? AND archived = 0 '
                        'AND tier IS NOT NULL', (media_type,))
        out = []
        for row in rows:
            if not scorer.is_contested(row['elo_score'], row['elo_sigma'],
                                       row['tier'], gate=gate):
                continue
            item = catalog.serialize(row)
            item['drift'] = abs(scorer.boundary_margin(row['elo_score'], row['tier']) or 0)
            out.append(item)
        out.sort(key=lambda i: (i['provisional'], -i['drift']))
        return jsonify({'type': media_type, 'items': out, 'gate': gate})

    @app.get('/api/rank/history')
    def rank_history():
        """Recorded rounds, newest first, with the ordering reconstructed.

        Rounds are stored as every pair; the weak ordering is rebuilt from those
        pairs rather than cached, so a correction made here flows straight back
        into scoring instead of silently disagreeing with a stored tier.
        """
        media_type = media_types.get(request.args.get('type')).key
        limit = min(int(request.args.get('limit') or 25), 200)
        sets = db.query('SELECT * FROM rank_sets WHERE media_type = ? '
                        'ORDER BY id DESC LIMIT ?', (media_type, limit))
        if not sets:
            return jsonify({'type': media_type, 'rounds': []})

        set_ids = [int(s['id']) for s in sets]
        marks = ','.join('?' * len(set_ids))
        rows = db.query(
            f'SELECT * FROM matches WHERE set_id IN ({marks}) ORDER BY id', set_ids)

        by_set = {}
        item_ids = set()
        for row in rows:
            by_set.setdefault(int(row['set_id']), []).append(row)
            item_ids.update((int(row['winner_id']), int(row['loser_id'])))

        marks = ','.join('?' * len(item_ids))
        titles = {int(r['id']): {'id': int(r['id']), 'title': r['title'],
                                 'year': r['year'], 'poster_url': r['poster_url'],
                                 'tier': r['tier'], 'elo_score': r['elo_score']}
                  for r in db.query(
                      f'SELECT id, title, year, poster_url, tier, elo_score '
                      f'FROM items WHERE id IN ({marks})', list(item_ids))}

        rounds = []
        for entry in sets:
            set_rows = by_set.get(int(entry['id']), [])
            ordering = scorer.tiers_from_pairs(
                [(int(r['winner_id']), int(r['loser_id']), bool(r['is_tie']))
                 for r in set_rows])
            upsets = [
                {'match_id': int(r['id']),
                 'winner': titles.get(int(r['winner_id'])),
                 'loser': titles.get(int(r['loser_id'])),
                 'is_tie': bool(r['is_tie'])}
                for r in set_rows
                if titles.get(int(r['winner_id']), {}).get('tier')
                and titles.get(int(r['loser_id']), {}).get('tier')
                and (titles[int(r['winner_id'])]['tier'] <
                     titles[int(r['loser_id'])]['tier'])]
            rounds.append({
                'set_id': int(entry['id']),
                'created_at': entry['created_at'],
                'size': entry['size'],
                'ordering': [[titles.get(i) for i in tier] for tier in ordering],
                'upsets': upsets,
            })
        return jsonify({'type': media_type, 'rounds': rounds})

    @app.get('/api/rank/leaderboard')
    def leaderboard():
        media_type = media_types.get(request.args.get('type')).key
        rows = db.query(
            'SELECT * FROM items WHERE media_type = ? AND archived = 0 '
            'ORDER BY elo_score DESC, title ASC', (media_type,))
        return jsonify({'type': media_type,
                        'items': [catalog.serialize(r) for r in rows]})

    # ── SPA ──────────────────────────────────────────────────────────────────

    @app.get('/', defaults={'path': ''})
    @app.get('/<path:path>')
    def spa(path):
        dist = config.FRONTEND_DIST
        if not dist.exists():
            return ('<h1>Curator</h1><p>Frontend not built. Run '
                    '<code>npm --prefix frontend install &amp;&amp; '
                    'npm --prefix frontend run build</code>.</p>'), 200
        target = dist / path
        if path and target.is_file():
            return send_from_directory(dist, path)
        return send_from_directory(dist, 'index.html')

    @app.after_request
    def cors(response):
        # The dev server runs on another port; production is same-origin so this
        # costs nothing there.
        response.headers.setdefault('Access-Control-Allow-Origin', '*')
        response.headers.setdefault('Access-Control-Allow-Headers', 'Content-Type')
        response.headers.setdefault('Access-Control-Allow-Methods',
                                    'GET, POST, PATCH, DELETE, OPTIONS')
        return response

    return app
