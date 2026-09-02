"""Database-backed tests: contest isolation, recording, undo, purge.

**The isolation suite is the one that catches the silent leak.** Nothing else
would: every contest sits on the same 800-1200 scale seeded off the same 1-5
ladder, so a query that forgets its `media_type` filter returns entirely
plausible numbers — no crash, no NULL, nothing wrong to the eye.
"""

import pytest

from curator import db, media_types, ranking, scorer


def test_a_round_in_one_contest_does_not_move_another(make_item):
    """The silent-leak test. If this fails, every score in the app is suspect."""
    anime = [make_item('anime') for _ in range(4)]
    movies = [make_item('movie', tier=3) for _ in range(4)]

    before = {r['id']: r['elo_score'] for r in
              db.query("SELECT id, elo_score FROM items WHERE media_type = 'movie'")}

    ranking.record_ranking('anime', [[anime[0]], [anime[1]], [anime[2]], [anime[3]]])
    ranking.refit_all('movie')

    after = {r['id']: r['elo_score'] for r in
             db.query("SELECT id, elo_score FROM items WHERE media_type = 'movie'")}
    assert after == before
    for movie in movies:
        assert after[movie] == pytest.approx(scorer.seed_elo(3))


def test_load_observations_is_scoped_to_one_contest(make_item):
    anime = [make_item('anime') for _ in range(2)]
    tv = [make_item('tv') for _ in range(2)]
    ranking.record_ranking('anime', [[anime[0]], [anime[1]]])
    ranking.record_ranking('tv', [[tv[0]], [tv[1]]])

    pairs, sets, rounds = ranking.load_observations('anime')
    touched = {i for pair in pairs for i in pair[:2]} | {w for w, _ in sets}
    assert touched <= set(anime)
    assert set(rounds) == set(anime)


def test_a_round_may_not_mix_media_types(make_item):
    """A cross-contest judgement is exactly what splitting by type prevents.

    Rejected loudly rather than recorded, because afterwards it would be
    invisible — the row looks like any other.
    """
    anime = make_item('anime')
    movie = make_item('movie')
    with pytest.raises(ValueError, match='may not mix media types'):
        ranking.record_ranking('anime', [[anime], [movie]])
    assert db.one('SELECT COUNT(*) c FROM matches')['c'] == 0
    assert db.one('SELECT COUNT(*) c FROM rank_sets')['c'] == 0


def test_a_round_is_stored_as_every_pair_with_one_set_id(make_item):
    items = [make_item('movie') for _ in range(6)]
    result = ranking.record_ranking(
        'movie', [[items[0]], [items[1], items[2]], [items[3]], [items[4], items[5]]])
    assert result['pairs'] == 15                       # C(6,2)
    rows = db.query('SELECT * FROM matches WHERE set_id = ?', (result['set_id'],))
    assert len(rows) == 15
    assert sum(1 for r in rows if r['is_tie']) == 2     # two tied pairs
    assert {r['media_type'] for r in rows} == {'movie'}


def test_the_stored_ordering_is_exactly_recoverable(make_item):
    items = [make_item('tv') for _ in range(6)]
    ordering = [[items[0]], [items[1], items[2]], [items[3], items[4]], [items[5]]]
    result = ranking.record_ranking('tv', ordering)
    rows = db.query('SELECT winner_id, loser_id, is_tie FROM matches WHERE set_id = ?',
                    (result['set_id'],))
    rebuilt = scorer.tiers_from_pairs(
        [(int(r['winner_id']), int(r['loser_id']), bool(r['is_tie'])) for r in rows])
    assert [sorted(t) for t in rebuilt] == [sorted(t) for t in ordering]


def test_scores_follow_the_recorded_ordering(make_item):
    items = [make_item('movie') for _ in range(6)]
    ranking.record_ranking('movie', [[i] for i in items])
    scores = {int(r['id']): r['elo_score'] for r in
              db.query('SELECT id, elo_score FROM items WHERE media_type = "movie"')}
    ordered = [scores[i] for i in items]
    assert ordered == sorted(ordered, reverse=True)


def test_tied_titles_score_identically(make_item):
    a, b, c = (make_item('movie') for _ in range(3))
    ranking.record_ranking('movie', [[a], [b, c]])
    scores = {int(r['id']): r['elo_score'] for r in
              db.query('SELECT id, elo_score FROM items')}
    assert scores[b] == pytest.approx(scores[c])
    assert scores[a] > scores[b]


def test_a_ranking_round_counts_once_not_c_n_2_times(make_item):
    """`elo_rounds` is a count of human judgements, not of stored rows."""
    items = [make_item('anime') for _ in range(6)]
    ranking.record_ranking('anime', [[i] for i in items])
    rounds = {int(r['id']): r['elo_rounds'] for r in
              db.query('SELECT id, elo_rounds FROM items')}
    assert all(rounds[i] == 1 for i in items)


def test_a_title_cannot_appear_twice_in_one_round(make_item):
    a, b = make_item('movie'), make_item('movie')
    with pytest.raises(ValueError, match='cannot appear twice'):
        ranking.record_ranking('movie', [[a], [b, a]])


def test_undo_drops_the_whole_round_and_is_type_scoped(make_item):
    """The last round on Anime must not undo a Movies round recorded after it."""
    anime = [make_item('anime') for _ in range(4)]
    movies = [make_item('movie') for _ in range(4)]
    anime_set = ranking.record_ranking('anime', [[i] for i in anime])['set_id']
    movie_set = ranking.record_ranking('movie', [[i] for i in movies])['set_id']

    assert ranking.undo_last('anime') == anime_set
    assert db.one('SELECT COUNT(*) c FROM matches WHERE set_id = ?',
                  (anime_set,))['c'] == 0
    assert db.one('SELECT COUNT(*) c FROM matches WHERE set_id = ?',
                  (movie_set,))['c'] == 6     # C(4,2), untouched
    assert ranking.stats('movie')['rounds'] == 1


def test_undo_returns_titles_to_their_seed(make_item):
    items = [make_item('movie', tier=3) for _ in range(4)]
    ranking.record_ranking('movie', [[i] for i in items])
    ranking.undo_last('movie')
    for row in db.query('SELECT elo_score, elo_rounds FROM items'):
        assert row['elo_score'] == pytest.approx(1000.0)
        assert row['elo_rounds'] == 0


def test_undo_with_no_history_is_a_no_op(make_item):
    make_item('movie')
    assert ranking.undo_last('movie') is None


def test_correcting_a_result_reverses_the_scores(make_item):
    a, b = make_item('movie'), make_item('movie')
    ranking.record_ranking('movie', [[a], [b]])
    match = db.one('SELECT id FROM matches')
    ranking.correct_match(int(match['id']), winner_id=b)
    scores = {int(r['id']): r['elo_score'] for r in
              db.query('SELECT id, elo_score FROM items')}
    assert scores[b] > scores[a]


def test_purge_keeps_the_other_pairs_in_the_same_round(make_item):
    """A complete round-robin minus one member is still a complete round-robin.

    "B beat C" is a judgement the user made and it is untouched by the mistake
    about A — so the survivors keep their round count and their ordering.
    """
    items = [make_item('movie') for _ in range(6)]
    ranking.record_ranking('movie', [[i] for i in items])
    assert db.one('SELECT COUNT(*) c FROM matches')['c'] == 15

    summary = ranking.purge_item(items[0], apply=True)
    assert summary['rows_deleted'] == 5
    assert summary['rows_kept_in_those_sets'] == 10
    assert db.one('SELECT COUNT(*) c FROM matches')['c'] == 10

    survivors = {int(r['id']): r['elo_rounds'] for r in
                 db.query('SELECT id, elo_rounds FROM items')}
    assert survivors[items[0]] == 0
    assert all(survivors[i] == 1 for i in items[1:])


def test_purge_returns_the_title_to_a_cold_start(make_item):
    items = [make_item('movie', tier=4) for _ in range(4)]
    ranking.record_ranking('movie', [[i] for i in items])
    ranking.purge_item(items[0], apply=True)
    row = db.one('SELECT elo_score, elo_sigma, elo_rounds FROM items WHERE id = ?',
                 (items[0],))
    assert row['elo_score'] == pytest.approx(scorer.seed_elo(4))
    assert row['elo_sigma'] == pytest.approx(scorer.PRIOR_SD * scorer.ELO_PER_LOGIT,
                                             rel=1e-3)
    assert row['elo_rounds'] == 0


def test_purge_is_dry_run_by_default(make_item):
    items = [make_item('movie') for _ in range(4)]
    ranking.record_ranking('movie', [[i] for i in items])
    summary = ranking.purge_item(items[0])
    assert summary['applied'] is False
    assert summary['backup_path'] is None
    assert db.one('SELECT COUNT(*) c FROM matches')['c'] == 6


def test_purge_writes_a_backup_before_deleting(make_item):
    items = [make_item('movie') for _ in range(4)]
    ranking.record_ranking('movie', [[i] for i in items])
    summary = ranking.purge_item(items[0], apply=True)
    assert summary['backup_path']
    import json
    from pathlib import Path
    saved = json.loads(Path(summary['backup_path']).read_text(encoding='utf-8'))
    assert len(saved['matches']) == 3


# ── the audit pool ───────────────────────────────────────────────────────────

def test_audit_pool_prioritises_under_covered_titles(make_item):
    items = [make_item('movie', tier=3) for _ in range(10)]
    pool, priority = ranking.audit_pool('movie')
    assert len(pool) == 10
    assert all(p == 1.0 for p in priority)      # nothing has been looked at yet


def test_audit_pool_keeps_a_spot_check_trickle(make_item):
    """Retiring audited titles outright would make a wrong placement unfalsifiable."""
    items = [make_item('movie', tier=3) for _ in range(12)]
    for _ in range(scorer.AUDIT_ROUNDS_TARGET):
        ranking.record_ranking('movie', [[i] for i in items[:6]])

    pool, priority = ranking.audit_pool('movie')
    ids = {p['id'] for p in pool}
    covered = set(items[:6])
    # The six under-covered titles are all wanted...
    assert set(items[6:]) <= ids
    # ...and at least one already-covered title still gets served.
    assert ids & covered


def test_stats_separates_coverage_from_findings(make_item):
    items = [make_item('movie', tier=3) for _ in range(6)]
    stats = ranking.stats('movie')
    assert stats['total'] == 6
    assert stats['covered'] == 0
    assert stats['rounds'] == 0
    assert stats['contested'] == 0

    for _ in range(scorer.AUDIT_ROUNDS_TARGET):
        ranking.record_ranking('movie', [[i] for i in items])
    stats = ranking.stats('movie')
    assert stats['rounds'] == scorer.AUDIT_ROUNDS_TARGET
    assert stats['covered'] == 6
    assert stats['covered_pct'] == 100.0


def test_a_consistently_beaten_top_tier_title_becomes_contested(make_item):
    """The audit's actual finding: filed high, plays low."""
    impostor = make_item('movie', title='Filed Too High', tier=5)
    rest = [make_item('movie', tier=3) for _ in range(5)]
    for _ in range(6):
        ranking.record_ranking('movie', [[r] for r in rest] + [[impostor]])

    row = db.one('SELECT elo_score, elo_sigma, tier FROM items WHERE id = ?',
                 (impostor,))
    assert scorer.is_contested(row['elo_score'], row['elo_sigma'], row['tier'],
                               gate=scorer.CONTESTED_GATE)
    assert ranking.stats('movie')['contested_confident'] >= 1


def test_every_registered_type_has_its_own_isolated_contest(make_item):
    for media_type in media_types.ORDERED:
        items = [make_item(media_type.key) for _ in range(4)]
        ranking.record_ranking(media_type.key, [[i] for i in items])

    for media_type in media_types.ORDERED:
        pairs, _, rounds = ranking.load_observations(media_type.key)
        owned = {int(r['id']) for r in db.query(
            'SELECT id FROM items WHERE media_type = ?', (media_type.key,))}
        assert set(rounds) <= owned
        assert ranking.stats(media_type.key)['rounds'] == 1
