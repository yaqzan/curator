"""Pure-math tests for the scorer. No database, no fixtures.

These pin the properties the batch fit was adopted *for* — order independence,
transitive propagation, a trustworthy sigma — plus the one shortcut that must
never be taken (C(n,2) independent duels).
"""

import numpy as np
import pytest

from curator import scorer


def test_no_observations_lands_exactly_on_the_tier_seed():
    """The cold-start guarantee: an uncompared title sits on its seed.

    This is what makes the fit safe to run on a corpus nobody has ranked yet —
    it can never be worse than not having run it.
    """
    fit = scorer.fit_scores([1, 2, 3], [1, 3, 5])
    assert fit.scores[1] == pytest.approx(800.0)
    assert fit.scores[2] == pytest.approx(1000.0)
    assert fit.scores[3] == pytest.approx(1200.0)
    assert fit.converged


def test_untiered_titles_seed_neutral():
    fit = scorer.fit_scores([1, 2], [None, 0])
    assert fit.scores[1] == pytest.approx(1000.0)
    assert fit.scores[2] == pytest.approx(1000.0)


def test_a_win_moves_both_titles_the_right_way():
    fit = scorer.fit_scores([1, 2], [3, 3], pairs=[(1, 2, False)])
    assert fit.scores[1] > 1000.0 > fit.scores[2]


def test_tie_is_symmetric():
    fit = scorer.fit_scores([1, 2], [3, 3], pairs=[(1, 2, True)])
    assert fit.scores[1] == pytest.approx(fit.scores[2])
    assert fit.scores[1] == pytest.approx(1000.0)


def test_transitive_propagation_without_a_direct_meeting():
    """A beats B, B beats C => C drops, though C never faced A.

    One of the three reasons this is a batch fit rather than online ELO.
    """
    fit = scorer.fit_scores([1, 2, 3], [3, 3, 3],
                            pairs=[(1, 2, False), (2, 3, False)])
    assert fit.scores[1] > fit.scores[2] > fit.scores[3]


def test_order_independence():
    """The answer must not depend on the sequence comparisons arrived in."""
    pairs = [(1, 2, False), (2, 3, False), (3, 1, True), (1, 3, False)]
    first = scorer.fit_scores([1, 2, 3], [3, 3, 3], pairs=pairs)
    second = scorer.fit_scores([1, 2, 3], [3, 3, 3], pairs=list(reversed(pairs)))
    for item in (1, 2, 3):
        assert first.scores[item] == pytest.approx(second.scores[item], abs=1e-4)


def test_more_evidence_shrinks_sigma():
    few = scorer.fit_scores([1, 2], [3, 3], pairs=[(1, 2, False)])
    many = scorer.fit_scores([1, 2], [3, 3], pairs=[(1, 2, False)] * 20)
    assert many.sigmas[1] < few.sigmas[1]


@pytest.mark.parametrize('size, expected', [(4, 1.5), (6, 2.0), (9, 2.7)])
def test_all_pairs_shortcut_would_overstate_the_evidence(size, expected):
    """The trap: reading a ranking round's stored pairs as independent duels.

    A round is *stored* as every pair, but it is ONE human judgement. Feeding
    those C(n,2) rows to the fit as independent duels is a different, wrong
    likelihood. Point estimates survive it; `sigma` does not, and the calibration
    gate runs on sigma — every title would read as settled on roughly half the
    evidence actually required.

    Measured in **information** (precision gained over the prior), not raw sigma:
    sigma compresses the gap badly because after a single round the Gaussian
    prior still dominates the posterior width, which is exactly what makes this
    failure mode invisible by eye. Inflation comes out at 1.5x / 2.0x / 2.7x for
    sets of 4 / 6 / 9 — matching the 1.6x / 2.1x / 2.9x measured on the original
    corpus this scorer came from.
    """
    ordering = [[i] for i in range(1, size + 1)]
    ids = range(1, size + 1)
    prior_info = 1.0 / (scorer.PRIOR_SD * scorer.ELO_PER_LOGIT) ** 2

    sets, ties = scorer.decompose_ranking(ordering)
    proper = scorer.fit_scores(ids, [3] * size, pairs=ties, sets=sets)

    all_pairs = [(a[0], b[0], False)
                 for i, a in enumerate(ordering) for b in ordering[i + 1:]]
    shortcut = scorer.fit_scores(ids, [3] * size, pairs=all_pairs)

    def gained(fit):
        return np.mean([1.0 / fit.sigmas[i] ** 2 - prior_info for i in ids])

    inflation = gained(shortcut) / gained(proper)
    assert inflation == pytest.approx(expected, abs=0.15), (
        f'n={size}: shortcut claims {inflation:.2f}x the information')


def test_last_tier_contributes_no_plackett_luce_term():
    """Nothing below the last tier was passed over, so it teaches nothing."""
    sets, _ = scorer.decompose_ranking([[1], [2], [3]])
    winners = [w for w, _ in sets]
    assert 3 not in winners
    assert sorted(winners) == [1, 2]


def test_ranking_decomposition_round_trips_through_stored_pairs():
    """Store a weak ordering as pairs, rebuild it, get the same ordering back."""
    ordering = [[1], [2, 3], [4], [5, 6]]
    rows = []
    for hi, tier in enumerate(ordering):
        for lo in range(hi, len(ordering)):
            for a in tier:
                for b in ordering[lo]:
                    if hi == lo:
                        if a < b:
                            rows.append((a, b, True))
                    else:
                        rows.append((a, b, False))
    assert scorer.tiers_from_pairs(rows) == ordering


def test_ties_group_together_when_rebuilding():
    rows = [(1, 2, True), (1, 3, False), (2, 3, False)]
    assert scorer.tiers_from_pairs(rows) == [[1, 2], [3]]


def test_setwise_information_beats_duel_information():
    """The reason the surface orders six rather than picking a winner from two."""
    duel = scorer.fit_scores([1, 2], [3, 3], pairs=[(1, 2, False)])
    sets, ties = scorer.decompose_ranking([[1], [2], [3], [4], [5], [6]])
    ranked = scorer.fit_scores(range(1, 7), [3] * 6, pairs=ties, sets=sets)
    prior = scorer.PRIOR_SD * scorer.ELO_PER_LOGIT
    duel_info = sum(prior - duel.sigmas[i] for i in (1, 2))
    rank_info = sum(prior - ranked.sigmas[i] for i in range(1, 7))
    assert rank_info > duel_info


def test_boundary_and_contested_arithmetic():
    assert scorer.tier_bounds(3) == (950.0, 1050.0)
    assert scorer.tier_bounds(1)[0] is None
    assert scorer.tier_bounds(5)[1] is None
    assert scorer.boundary_margin(1000.0, 3) == pytest.approx(50.0)
    assert scorer.boundary_margin(940.0, 3) == pytest.approx(-10.0)
    assert scorer.is_contested(940.0, 20.0, 3) is True
    # 10 ELO out with a 60 ELO sigma is drift, not a finding.
    assert scorer.is_contested(940.0, 60.0, 3, gate=scorer.CONTESTED_GATE) is False
    assert scorer.is_contested(900.0, 60.0, 3, gate=scorer.CONTESTED_GATE) is True


def test_untiered_is_never_contested_or_settled():
    assert scorer.is_contested(1400.0, 10.0, None) is False
    assert scorer.is_settled(1000.0, 1.0, None) is False


def test_implied_tier_maps_scores_back_to_the_ladder():
    assert scorer.implied_tier(800.0) == 1
    assert scorer.implied_tier(1000.0) == 3
    assert scorer.implied_tier(1400.0) == 5


def test_select_set_respects_the_one_tier_fence():
    """A tier-1 against a tier-5 is a foregone conclusion and teaches nothing."""
    pool = ([{'id': i, 'tier': 1, 'elo_score': 800.0, 'elo_sigma': 70.0}
             for i in range(1, 15)] +
            [{'id': i, 'tier': 5, 'elo_score': 1200.0, 'elo_sigma': 70.0}
             for i in range(15, 30)])
    for seed in range(25):
        chosen = scorer.select_set(pool, size=4, rng=np.random.default_rng(seed))
        tiers = {c['tier'] for c in chosen}
        assert max(tiers) - min(tiers) <= 1


def test_select_set_mixes_cross_tier_and_same_tier():
    """The boundary bias is a *bias*, not a rule.

    Rounding the quota made every duel cross-tier, which stops within-tier
    ordering ever being measured — the opposite failure to the 86.8%-intra-tier
    pairing it was introduced to fix.
    """
    pool = [{'id': i, 'tier': 2 + (i % 2), 'elo_score': 900.0 + 100 * (i % 2),
             'elo_sigma': 70.0} for i in range(1, 40)]
    cross = same = 0
    for seed in range(60):
        chosen = scorer.select_set(pool, size=2, rng=np.random.default_rng(seed))
        if chosen[0]['tier'] == chosen[1]['tier']:
            same += 1
        else:
            cross += 1
    assert cross > 0 and same > 0, f'cross={cross} same={same}'


def test_select_set_returns_everything_when_the_pool_is_small():
    pool = [{'id': i, 'tier': 3, 'elo_score': 1000.0, 'elo_sigma': 70.0}
            for i in range(1, 4)]
    assert len(scorer.select_set(pool, size=6)) == 3


def test_priority_steers_selection_where_sigma_cannot():
    """On a thin corpus sigma cannot discriminate — the priority flag must.

    Titles with 0 rounds and titles with 3 differ by ~1 ELO of posterior width,
    so ranking by sigma alone hands already-audited titles far more slots than
    their intended trickle.
    """
    pool = [{'id': i, 'tier': 3, 'elo_score': 1000.0, 'elo_sigma': 70.0 + (i % 3)}
            for i in range(1, 41)]
    priority = [1.0 if item['id'] <= 8 else 0.0 for item in pool]
    hits = 0
    for seed in range(40):
        chosen = scorer.select_set(pool, size=6, priority=priority,
                                   rng=np.random.default_rng(seed))
        hits += sum(1 for c in chosen if c['id'] <= 8)
    # 8 of 40 titles are wanted; blind selection would land ~48 of 240 slots.
    assert hits > 90, f'priority barely steered selection: {hits}/240'
