"""Comparison history: the shared loader/refit, recording, undo and purge.

Every function here takes a `media_type` and touches only that contest's rows and
columns. All contests share the `matches` table; **the filter is what keeps them
separate, and omitting it is undetectable by eye** because every contest lives on
the same 800-1200 scale seeded off a 1-5 tier. A query that forgets returns
perfectly plausible numbers.

This module is the ONE definition of "read the history and refit everyone."
Do not re-inline a private copy in the API or a script — a divergent copy is
exactly how the C(n,2)-as-independent-duels mistake creeps back in, and that one
silently wrecks `elo_sigma`, which the calibration gate runs on.
"""

from __future__ import annotations

import collections
import json
import time
from pathlib import Path

from . import config, db, media_types, scorer


def load_observations(media_type):
    """Read one contest's history in the form the batch fit wants.

    Rows carrying a `set_id` came from ONE ranking round — the user ordered N
    titles into tiers, and every pair in that set was written out. They are
    reassembled into the original weak ordering (`scorer.tiers_from_pairs`) and
    decomposed into proper Plackett-Luce terms plus within-tier ties.

    Returns `(pairs, sets, rounds)` where `rounds` maps item_id -> the set of
    round keys it took part in (a ranking set counts once, not C(n,2) times).
    """
    rows = db.query(
        'SELECT id, winner_id, loser_id, is_tie, set_id FROM matches '
        'WHERE media_type = ?', (media_type,))

    pairs = []
    by_set = collections.defaultdict(list)
    rounds = {}
    for row in rows:
        wid, lid, tie = int(row['winner_id']), int(row['loser_id']), bool(row['is_tie'])
        sid = row['set_id']
        key = ('s', int(sid)) if sid is not None else ('m', int(row['id']))
        rounds.setdefault(wid, set()).add(key)
        rounds.setdefault(lid, set()).add(key)
        if sid is None:
            pairs.append((wid, lid, tie))
        else:
            by_set[int(sid)].append((wid, lid, tie))

    sets = []
    for set_rows in by_set.values():
        round_sets, round_pairs = scorer.decompose_ranking(
            scorer.tiers_from_pairs(set_rows))
        sets.extend(round_sets)
        pairs.extend(round_pairs)
    return pairs, sets, rounds


def refit_all(media_type):
    """Recompute every title's score + posterior SD in ONE contest.

    The exact MAP estimate given every comparison on record for that contest,
    independent of the order they arrived in. Cheap enough to run on every
    submission. Returns the `scorer.Fit`, or None if the contest is empty.
    """
    rows = db.query(
        'SELECT id, tier FROM items WHERE media_type = ? AND archived = 0',
        (media_type,))
    if not rows:
        return None

    ids = [int(r['id']) for r in rows]
    tiers = [r['tier'] for r in rows]
    pairs, sets, rounds = load_observations(media_type)
    fit = scorer.fit_scores(ids, tiers, pairs=pairs, sets=sets)

    conn = db.connect()
    with conn:
        conn.executemany(
            'UPDATE items SET elo_score = ?, elo_sigma = ?, elo_rounds = ?, '
            'updated_at = ? WHERE id = ?',
            [(fit.scores[i], fit.sigmas[i], len(rounds.get(i, ())), int(time.time()), i)
             for i in ids])
    return fit


def refit_everything():
    """Refit every contest. For CLI use after a bulk tier edit or an import."""
    return {t.key: refit_all(t.key) for t in media_types.ORDERED}


# ── recording a round ────────────────────────────────────────────────────────

def record_ranking(media_type, tiers):
    """Store one ranking round and refit.

    `tiers` is a list of lists of item ids, best first; ids sharing a list are
    tied. Written out as EVERY pair in the set sharing one `set_id`, with
    `is_tie` set for same-tier pairs — which keeps History/undo/correction
    working over one uniform table and makes the ordering exactly recoverable.

    Every member must belong to `media_type`. Mixing contests in one round is
    rejected rather than silently recorded: a cross-contest judgement is exactly
    the thing splitting by type exists to prevent, and it would be invisible
    afterwards.
    """
    flat = [int(i) for tier in tiers for i in tier]
    if len(flat) < 2:
        raise ValueError('a ranking round needs at least two titles')
    if len(set(flat)) != len(flat):
        raise ValueError('a title cannot appear twice in one round')

    placeholders = ','.join('?' * len(flat))
    owned = db.query(
        f'SELECT id, media_type FROM items WHERE id IN ({placeholders})', flat)
    found = {int(r['id']): r['media_type'] for r in owned}
    missing = [i for i in flat if i not in found]
    if missing:
        raise ValueError(f'unknown item ids: {missing}')
    wrong = [i for i, t in found.items() if t != media_type]
    if wrong:
        raise ValueError(
            f'items {wrong} are not in the {media_type} contest — '
            'a round may not mix media types')

    now = int(time.time())
    conn = db.connect()
    with conn:
        cur = conn.execute(
            'INSERT INTO rank_sets(media_type, size, created_at) VALUES (?, ?, ?)',
            (media_type, len(flat), now))
        set_id = int(cur.lastrowid)

        rows = []
        for hi in range(len(tiers)):
            for lo in range(hi, len(tiers)):
                for a in tiers[hi]:
                    for b in tiers[lo]:
                        if hi == lo:
                            if int(a) >= int(b):
                                continue          # one row per within-tier pair
                            rows.append((media_type, set_id, int(a), int(b), 1, now))
                        else:
                            rows.append((media_type, set_id, int(a), int(b), 0, now))
        conn.executemany(
            'INSERT INTO matches(media_type, set_id, winner_id, loser_id, is_tie, '
            'created_at) VALUES (?, ?, ?, ?, ?, ?)', rows)

    fit = refit_all(media_type)
    return {'set_id': set_id, 'pairs': len(rows), 'fit_converged': bool(fit and fit.converged)}


def undo_last(media_type):
    """Drop the most recent round in ONE contest and refit.

    Undo is set-aware (it drops the whole round, never a partial ranking) and
    contest-scoped: the last round on Anime must not undo a Movies round recorded
    after it.
    """
    row = db.one(
        'SELECT id FROM rank_sets WHERE media_type = ? ORDER BY id DESC LIMIT 1',
        (media_type,))
    if not row:
        return None
    set_id = int(row['id'])
    conn = db.connect()
    with conn:
        conn.execute('DELETE FROM matches WHERE set_id = ?', (set_id,))
        conn.execute('DELETE FROM rank_sets WHERE id = ?', (set_id,))
    refit_all(media_type)
    return set_id


def correct_match(match_id, winner_id=None, tie=None):
    """Fix one stored result (mis-click on the History tab), then refit.

    Swaps `winner_id`/`loser_id` and/or rewrites `is_tie`. `is_tie` is an explicit
    column on purpose — under a batch fit it cannot be reconstructed by inverting
    an online-ELO delta, because the fit rewrites the scores wholesale.
    """
    row = db.one('SELECT * FROM matches WHERE id = ?', (match_id,))
    if not row:
        return None
    w, l = int(row['winner_id']), int(row['loser_id'])
    if winner_id is not None:
        winner_id = int(winner_id)
        if winner_id not in (w, l):
            raise ValueError('winner must be one of the two titles in the match')
        if winner_id == l:
            w, l = l, w
    is_tie = int(bool(row['is_tie']) if tie is None else bool(tie))
    db.execute('UPDATE matches SET winner_id = ?, loser_id = ?, is_tie = ? WHERE id = ?',
               (w, l, is_tie, match_id))
    refit_all(row['media_type'])
    return {'id': int(match_id), 'winner_id': w, 'loser_id': l, 'is_tie': bool(is_tie)}


# ── purge ────────────────────────────────────────────────────────────────────

PURGE_BACKUP_DIR = config.DATA_DIR / 'purges'


def purge_item(item_id, apply=False):
    """Delete every comparison one title took part in, then refit.

    For when the rounds themselves were judged on wrong information — you had it
    confused with something else, or rated the wrong entry in a franchise.

    **Only this title's own pair rows are deleted — the rest of each round stays
    (don't "fix" this).** "B beat C" is a judgement the user made and it is
    untouched by the mistake about A. A complete round-robin minus one member is
    still a complete round-robin, so `tiers_from_pairs` recovers the survivors'
    weak ordering exactly. After the refit this title has no observations left, so
    the Gaussian prior lands it exactly on its tier seed at full `PRIOR_SD` — a
    cold start indistinguishable from something nobody ever compared.

    Dry-run by default. `--apply` writes the deleted rows to
    `data/purges/<ts>-item<id>.json` **before** deleting; re-inserting that file
    is the only way back.
    """
    item = db.one('SELECT * FROM items WHERE id = ?', (item_id,))
    if not item:
        return None
    media_type = item['media_type']

    rows = db.query(
        'SELECT m.id, m.set_id, m.is_tie, m.created_at, '
        '       m.winner_id, w.title AS winner_title, '
        '       m.loser_id,  l.title AS loser_title '
        'FROM matches m '
        'LEFT JOIN items w ON w.id = m.winner_id '
        'LEFT JOIN items l ON l.id = m.loser_id '
        'WHERE m.media_type = ? AND (m.winner_id = ? OR m.loser_id = ?) '
        'ORDER BY m.id', (media_type, item_id, item_id))
    matches = [dict(r) for r in rows]

    set_ids = sorted({m['set_id'] for m in matches if m['set_id'] is not None})
    duels = sum(1 for m in matches if m['set_id'] is None)
    survivors = 0
    if set_ids:
        ph = ','.join('?' * len(set_ids))
        survivors = int(db.one(
            f'SELECT COUNT(*) c FROM matches WHERE set_id IN ({ph}) '
            'AND winner_id <> ? AND loser_id <> ?',
            (*set_ids, item_id, item_id))['c'])

    summary = {
        'item_id': int(item_id),
        'title': item['title'],
        'media_type': media_type,
        'applied': bool(apply),
        'rows_deleted': len(matches),
        'rounds_deleted': len(set_ids) + duels,
        'sets': set_ids,
        'rows_kept_in_those_sets': survivors,
        'before': {'score': item['elo_score'], 'sigma': item['elo_sigma'],
                   'rounds': item['elo_rounds']},
        'matches': list(reversed(matches)),
        'backup_path': None,
        'after': None,
    }
    if not apply or not matches:
        return summary

    PURGE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    backup = PURGE_BACKUP_DIR / f'{stamp}-item{item_id}.json'
    backup.write_text(json.dumps(
        {'item_id': int(item_id), 'title': item['title'], 'media_type': media_type,
         'purged_at': stamp, 'matches': matches}, indent=2, default=str),
        encoding='utf-8')
    summary['backup_path'] = str(backup)

    db.execute('DELETE FROM matches WHERE media_type = ? AND (winner_id = ? OR loser_id = ?)',
               (media_type, item_id, item_id))
    refit_all(media_type)

    fresh = db.one('SELECT elo_score, elo_sigma, elo_rounds, tier FROM items WHERE id = ?',
                   (item_id,))
    summary['after'] = {
        'score': fresh['elo_score'], 'sigma': fresh['elo_sigma'],
        'rounds': fresh['elo_rounds'],
        'seed_for_tier': round(scorer.seed_elo(fresh['tier']), 1),
    }
    return summary


# ── the pool a round is drawn from ───────────────────────────────────────────

# Share of each round drawn from titles that have already been audited and sit
# comfortably mid-tier. NOT optional: retiring audited titles outright would make
# a confidently-wrong placement unfalsifiable, since nothing could ever
# contradict it again.
SPOT_CHECK_RATE = 0.12


def audit_pool(media_type, rng=None):
    """Candidates for the next round, plus a priority flag per candidate.

    Priority order: contested titles -> under-covered titles -> a
    `SPOT_CHECK_RATE` trickle of the rest, drawn from those sitting closest to a
    tier boundary. A title that has been looked at and sits comfortably mid-tier
    drops out; serving it again buys nothing.

    **Pool membership alone does NOT steer selection — the priority flag is
    load-bearing (don't drop it).** On a thin corpus sigma cannot discriminate:
    titles with 0 rounds and titles with 3 differ by ~1 ELO of posterior width.
    Ranking by sigma alone let already-audited titles take ~43% of slots when
    they were meant to be a 12% trickle, because the boundary-biased fill hunts
    exactly where those titles sit.
    """
    import numpy as np
    rng = rng or np.random.default_rng()

    rows = [dict(r) for r in db.query(
        'SELECT id, title, year, tier, elo_score, elo_sigma, elo_rounds, poster_url '
        'FROM items WHERE media_type = ? AND archived = 0', (media_type,))]
    if not rows:
        return [], []

    wanted, spare = [], []
    for row in rows:
        score, sigma, tier = row['elo_score'], row['elo_sigma'], row['tier']
        if scorer.is_contested(score, sigma, tier, gate=scorer.CONTESTED_GATE):
            wanted.append(row)
        elif (row['elo_rounds'] or 0) < scorer.AUDIT_ROUNDS_TARGET:
            wanted.append(row)
        else:
            spare.append(row)

    # The trickle: the spare titles sitting nearest a tier line, since those are
    # the ones a further round could still move.
    if spare:
        take = max(1, int(round(len(rows) * SPOT_CHECK_RATE)))
        spare.sort(key=lambda r: (scorer.boundary_margin(r['elo_score'], r['tier'])
                                  if r['tier'] else 1e9))
        wanted_ids = {r['id'] for r in wanted}
        trickle = [r for r in spare[:take * 3] if r['id'] not in wanted_ids][:take]
    else:
        trickle = []

    pool = wanted + trickle
    priority = [1.0] * len(wanted) + [0.0] * len(trickle)
    return pool, priority


def stats(media_type):
    """Progress signals for one contest. Three distinct ones — don't collapse.

    * `covered` — titles with at least `AUDIT_ROUNDS_TARGET` rounds. This is
      COVERAGE ("has it been looked at properly"), not a claim about precision.
    * `settled` — the posterior sits a full sigma clear of the nearest tier
      boundary. **Secondary signal only; it must never drive a progress bar** —
      it is not reachable for every title, so the bar crawls toward a low ceiling
      and never fills, implying permanent failure when the work is actually done.
    * `contested` / `contested_confident` — the score has drifted OUT of the
      filed tier. This is the audit's actual FINDING, and the confident count is
      the stopping signal: while it keeps producing titles there is real signal
      left; once only marginal crossings remain, further re-tiering does damage.
    """
    rows = db.query(
        'SELECT tier, elo_score, elo_sigma, elo_rounds FROM items '
        'WHERE media_type = ? AND archived = 0', (media_type,))
    total = len(rows)
    covered = settled = contested = confident = untiered = 0
    for r in rows:
        if not r['tier']:
            untiered += 1
        if (r['elo_rounds'] or 0) >= scorer.AUDIT_ROUNDS_TARGET:
            covered += 1
        if scorer.is_settled(r['elo_score'], r['elo_sigma'], r['tier']):
            settled += 1
        if scorer.is_contested(r['elo_score'], r['elo_sigma'], r['tier']):
            contested += 1
        if scorer.is_contested(r['elo_score'], r['elo_sigma'], r['tier'],
                               gate=scorer.CONTESTED_GATE):
            confident += 1

    rounds = db.one('SELECT COUNT(*) c FROM rank_sets WHERE media_type = ?',
                    (media_type,))['c']
    return {
        'media_type': media_type,
        'total': total,
        'untiered': untiered,
        'rounds': int(rounds),
        'covered': covered,
        'covered_pct': round(100.0 * covered / total, 1) if total else 0.0,
        'settled': settled,
        'contested': contested,
        'contested_confident': confident,
        'audit_rounds_target': scorer.AUDIT_ROUNDS_TARGET,
        'rankable': total,
    }
