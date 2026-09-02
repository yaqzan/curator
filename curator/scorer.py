"""Batch Bradley-Terry / Plackett-Luce scorer.

Ported from Archivist's `archiver/local_tube/face_rank.py`, which is the version
of this that has actually been measured against a real corpus. Everything below
about why it is shaped this way was learned there; the numbers quoted are from
that corpus (975 subjects, ~1k comparisons) and carry over because the model is
the same. Only the domain changed.

SHARED BY EVERY MEDIA TYPE. Nothing in here knows or cares what is being judged:
every function takes a bare 1-5 `tier` scalar or a list of plain dicts.
`curator/media_types.py` owns the mapping — do not hardcode a type key in here.

Not online ELO (don't silently revert)
--------------------------------------
This is a single MAP fit over the *entire* comparison history, re-run after every
submission — not a `K=32` incremental nudge. The reason is **not** raw accuracy:
a simulation put online ELO, Glicko-style adaptive-K and Bradley-Terry within
0.01 Spearman of each other. The batch fit was adopted for three structural
properties an online rule cannot give you:

  1. **Order independence.** Online ELO's answer depends on the sequence
     comparisons happened to arrive in. A batch fit doesn't.
  2. **Transitive propagation.** If A beats B and B beats C, C's score drops
     immediately even though C never faced A. Online ELO needs many more
     comparisons to get there.
  3. **A real uncertainty estimate.** The Laplace-approximation posterior SD
     (`sigma`) gives a principled "is this settled yet?" test, replacing an
     arbitrary match count.

Model
-----
Each title has a latent quality `theta` in logit units. For a comparison,

    P(i beats j)            = sigmoid(theta_i - theta_j)                 (pairwise)
    P(i best of set S)      = exp(theta_i) / sum_{j in S} exp(theta_j)   (Plackett-Luce)

A tie contributes half a win in each direction. The prior is Gaussian, centred on
the title's filed 1-5 tier:

    theta_i ~ N(m(tier_i), PRIOR_SD^2)

so a title with no comparisons sits exactly on its tier seed and the fit can
never be worse than the cold start. Display scores stay on the familiar ELO scale
(tier 1 -> 800 ... tier 5 -> 1200).

Optimiser
---------
Diagonal-Newton (Jacobi) ascent. The objective is strongly concave thanks to the
Gaussian prior, so the diagonal Hessian is bounded away from zero and the step is
always well-defined — no line search, no scipy, ~13 iterations, single-digit
milliseconds at corpus scale. The same diagonal gives the posterior SD for free.
"""

from __future__ import annotations

import math

import numpy as np

# ── scale constants ──────────────────────────────────────────────────────────
ELO_PER_LOGIT = 400.0 / math.log(10.0)     # 173.72
ELO_BASE = 1000.0

# The 1-5 tier ladder, shared by every contest: a `tier` of 4 seeds at 1100 in
# Movies exactly as it does in Anime. Both are the same kind of scale.
TIER_SEED_ELO = {1: 800.0, 2: 900.0, 3: 1000.0, 4: 1100.0, 5: 1200.0}
DEFAULT_SEED_ELO = 1000.0

# Prior SD in logit units. 0.45 ~= 78 ELO ~= three quarters of a tier.
#
# **A tight prior is self-defeating (don't tighten this back).** A prior that
# asserts "the filed tiers are already right" makes it nearly impossible for
# evidence to prove one wrong — which is the entire job. Measured detection of a
# subject filed one full tier too high, over 4 rounds:
#
#   PRIOR_SD   caught   false alarms
#     0.30       19%         4%
#     0.45       51%        17%
#     0.60       53%        24%
#     0.80       70%        35%
#
# 0.45 roughly triples recall for a manageable false-alarm rate. `contested` is a
# REVIEW QUEUE, not a verdict — a human looks at the title before any tier moves
# — so recall matters more than precision.
PRIOR_SD = 0.45

# How many posterior SDs clear of the nearest tier boundary before a placement
# counts as settled. 1.0 ~= 84% of the posterior on the correct side of the line.
#
# NOTE settling is NOT reachable for every title and must never be shown as a
# progress bar that is supposed to fill. Rounds needed, by distance from the
# nearest boundary: margin 50 ELO -> 1.6 rounds; 40 -> 13; 32 -> 31; 20 -> 109;
# 10 -> 491. A title genuinely sitting on a tier line IS genuinely borderline,
# and no volume of comparison resolves which side it "should" be on.
TIER_BOUNDARY_Z = 1.0

# Rounds a title needs before its tier counts as audited. A COVERAGE target —
# "has this been looked at properly" — not a claim about score precision.
# Detection of a title filed a full tier too high was 22% at 2 rounds and 51% at
# 4. Sweep cost scales linearly with this, so 4 is the compromise between real
# power and a sweep that finishes.
AUDIT_ROUNDS_TARGET = 4

# Sigmas past a tier line before a crossing counts as a real finding rather than
# boundary drift.
CONTESTED_GATE = 0.25

# Tier boundaries sit midway between the seeds: 850 / 950 / 1050 / 1150.
TIER_HALF_WIDTH = 50.0

_MAX_ITERS = 300
_TOL = 1e-7
_DAMPING = 0.9      # under-relax; diagonal-Newton ignores off-diagonal coupling,
                    # so a full step can overshoot.


class Fit:
    __slots__ = ('scores', 'sigmas', 'iterations', 'converged')

    def __init__(self, scores, sigmas, iterations, converged):
        self.scores = scores
        self.sigmas = sigmas
        self.iterations = iterations
        self.converged = converged


def seed_elo(tier):
    """Starting ELO implied by a filed 1-5 tier (None/0 -> neutral 1000)."""
    return TIER_SEED_ELO.get(tier, DEFAULT_SEED_ELO)


def elo_to_logit(elo):
    return (float(elo) - ELO_BASE) / ELO_PER_LOGIT


def logit_to_elo(theta):
    return ELO_BASE + float(theta) * ELO_PER_LOGIT


def fit_scores(item_ids, tiers, pairs=(), sets=(), prior_sd=PRIOR_SD,
               max_iters=_MAX_ITERS, tol=_TOL):
    """Fit latent quality for every title in one contest.

    Args:
        item_ids: sequence of item ids, defining the index order.
        tiers:    parallel sequence of filed tiers (1-5, or None).
        pairs:    iterable of (winner_id, loser_id, is_tie).
        sets:     iterable of (winner_id, [member_ids]) Plackett-Luce top-1 picks.
        prior_sd: Gaussian prior SD in logit units.

    Returns:
        Fit(scores, sigmas, iterations, converged) — both maps are keyed by item
        id and live on the ELO scale.
    """
    item_ids = list(item_ids)
    n = len(item_ids)
    if n == 0:
        return Fit({}, {}, 0, True)

    index = {iid: i for i, iid in enumerate(item_ids)}
    prior = np.array([elo_to_logit(seed_elo(t)) for t in tiers], dtype=np.float64)
    lam = 1.0 / (prior_sd ** 2)

    # ── vectorise the pairwise observations ─────────────────────────────────
    w_idx, l_idx, weights = [], [], []
    for winner_id, loser_id, is_tie in pairs:
        wi, li = index.get(winner_id), index.get(loser_id)
        if wi is None or li is None or wi == li:
            continue
        if is_tie:
            # A tie is half a win each way — two half-weight observations.
            w_idx.extend((wi, li)); l_idx.extend((li, wi)); weights.extend((0.5, 0.5))
        else:
            w_idx.append(wi); l_idx.append(li); weights.append(1.0)

    W = np.asarray(w_idx, dtype=np.intp)
    L = np.asarray(l_idx, dtype=np.intp)
    PW = np.asarray(weights, dtype=np.float64)

    # ── bucket the setwise observations by size for vectorised evaluation ───
    by_size = {}
    for winner_id, member_ids in sets:
        members = [index[m] for m in member_ids if m in index]
        wi = index.get(winner_id)
        if wi is None or wi not in members or len(members) < 2:
            continue
        ordered = [wi] + [m for m in members if m != wi]   # winner in column 0
        by_size.setdefault(len(ordered), []).append(ordered)
    set_blocks = [np.asarray(rows, dtype=np.intp) for rows in by_size.values()]

    theta = prior.copy()
    hess = np.full(n, lam, dtype=np.float64)
    converged = False
    it = 0
    for it in range(1, max_iters + 1):
        grad = -lam * (theta - prior)
        hess = np.full(n, lam, dtype=np.float64)      # stored as -H (positive)

        if W.size:
            d = theta[W] - theta[L]
            p = 1.0 / (1.0 + np.exp(-np.clip(d, -60.0, 60.0)))
            resid = PW * (1.0 - p)
            np.add.at(grad, W, resid)
            np.add.at(grad, L, -resid)
            info = PW * p * (1.0 - p)
            np.add.at(hess, W, info)
            np.add.at(hess, L, info)

        for block in set_blocks:
            th = theta[block]                          # (n_sets, size)
            th = th - th.max(axis=1, keepdims=True)
            e = np.exp(th)
            sm = e / e.sum(axis=1, keepdims=True)
            g = -sm
            g[:, 0] += 1.0                             # winner is column 0
            np.add.at(grad, block.ravel(), g.ravel())
            np.add.at(hess, block.ravel(), (sm * (1.0 - sm)).ravel())

        step = _DAMPING * (grad / hess)
        theta += step
        if np.max(np.abs(step)) < tol:
            converged = True
            break

    scores = {iid: logit_to_elo(theta[i]) for i, iid in enumerate(item_ids)}
    sig = 1.0 / np.sqrt(hess)
    sigmas = {iid: float(sig[i]) * ELO_PER_LOGIT for i, iid in enumerate(item_ids)}
    return Fit(scores, sigmas, it, converged)


def tier_bounds(tier):
    """(low, high) ELO band a 1-5 tier owns. None = open-ended."""
    if not tier:
        return (None, None)
    seed = seed_elo(tier)
    low = None if tier <= 1 else seed - TIER_HALF_WIDTH
    high = None if tier >= 5 else seed + TIER_HALF_WIDTH
    return (low, high)


def boundary_margin(score_elo, tier):
    """ELO distance from `score_elo` to the nearest boundary of its own tier.

    Negative means the score has drifted OUT of the tier the title is filed
    under — a live discrepancy, not merely an unresolved one.
    """
    if not tier or score_elo is None:
        return None
    low, high = tier_bounds(tier)
    gaps = []
    if low is not None:
        gaps.append(float(score_elo) - low)
    if high is not None:
        gaps.append(high - float(score_elo))
    return min(gaps) if gaps else None


def implied_tier(score_elo):
    """Which tier this score lands in, ignoring what the title is filed under."""
    if score_elo is None:
        return None
    s = float(score_elo)
    for tier in (1, 2, 3, 4, 5):
        low, high = tier_bounds(tier)
        if (low is None or s >= low) and (high is None or s < high):
            return tier
    return 5


def is_contested(score_elo, sigma_elo, tier, gate=0.0):
    """Has the score left the tier the title is filed under?

    `gate` is how many sigmas past the line it must sit before it counts. gate=0
    answers "is it over the line at all" — dominated by boundary churn, because a
    title sitting within noise of a line crosses it and back as evidence trickles
    in. **Gate > 0 is the one that means something:** simulated against a judge
    with realistic noise, a corpus with real errors resolves 74-89% of them while
    a corpus with none left starts INVENTING about ten as the process shuffles
    boundary cases. The confidently-contested count is what separates the two.
    """
    if not tier or score_elo is None:
        return False
    margin = boundary_margin(score_elo, tier)
    if margin is None or margin >= 0:
        return False
    if not gate:
        return True
    if sigma_elo is None:
        return False
    return abs(margin) >= gate * float(sigma_elo)


def is_settled(score_elo, sigma_elo, tier):
    """Is this title's TIER PLACEMENT resolved? The calibration test.

    Decision-linked rather than a flat sigma threshold: the question the system
    exists to answer is "is this in the right tier", so what matters is whether
    the posterior sits clear of the nearest boundary — not whether the score is
    pinned to some arbitrary precision. Two properties a flat threshold lacked:
    effort self-targets on titles drifting toward a boundary, and a better prior
    actually helps instead of making the target recede just as fast.
    """
    if not tier or sigma_elo is None:
        return False
    margin = boundary_margin(score_elo, tier)
    if margin is None:
        return False
    return margin >= TIER_BOUNDARY_Z * float(sigma_elo)


# ── weak orderings ("rank these 6, ties allowed") ────────────────────────────
#
# A full ranking is worth far more than a top-1 pick from the same set, because
# it pins down every relation instead of just the winner's. Total Fisher
# information at equal scores, summed over participants:
#
#     duel (2)                 0.500     1.0x
#     top-1 of 6               0.833     1.7x
#     full ranking of 6        3.550     7.1x
#     full ranking of 9        6.171    12.3x
#
# Measured: ordering 6 reached in ~110 rounds what top-1 needed ~700 for, and
# top-1 could not reach rho 0.90 at any tested budget while ranking passed it
# comfortably. This holds even charging ranking several times the interaction
# time, because top-1 flattens out and ranking keeps climbing.
#
# Ties are load-bearing, not a convenience: forcing a pick between two titles the
# user reads as equal injects a coin flip into the data as though it were signal.
# "These are all the same to me" is a legitimate, accepted answer.

def decompose_ranking(tiers):
    """Turn a weak ordering into observations the fit already understands.

    `tiers` is a list of lists, best first; items sharing a list are tied.
    Returns (sets, pairs) for `fit_scores`:

      * **sets** — Plackett-Luce top-1 choices from a SHRINKING pool. Each item in
        tier m is recorded as chosen from the pool of everyone in tier m and
        below. The last tier contributes nothing: there is nothing below it to
        have been preferred over.
      * **pairs** — one tie per pair inside a tier.

    DO NOT replace this with the obvious shortcut of emitting all C(n,2) ordered
    pairs as independent duels. That is a different (wrong) likelihood which
    overstates the information in one human judgement by 1.6x at n=4, 2.1x at
    n=6 and 2.9x at n=9. Point estimates survive it; `sigma` does not, and the
    calibration gate runs on sigma — every title would read as settled on half
    the evidence actually required.
    """
    tiers = [list(t) for t in tiers if t]
    sets, pairs = [], []

    pool = [g for tier in tiers for g in tier]
    for idx, tier in enumerate(tiers):
        if idx < len(tiers) - 1:
            for item in tier:
                sets.append((item, list(pool)))
            remaining = set(tier)
            pool = [g for g in pool if g not in remaining]
        for a in range(len(tier)):
            for b in range(a + 1, len(tier)):
                pairs.append((tier[a], tier[b], True))
    return sets, pairs


def tiers_from_pairs(rows):
    """Rebuild the weak ordering a stored comparison set came from.

    `rows` is that set's (winner_id, loser_id, is_tie) rows. A round is written
    out as every pair in the set, so the ordering is fully recoverable: score
    each item 1 per win and 0.5 per tie, then group by score. For a genuine weak
    ordering this is exact — two items score equally iff they shared a tier.

    Reconstructing rather than storing a tier column keeps the pairwise rows the
    single source of truth, so correcting one result from the History tab feeds
    straight back into scoring instead of silently disagreeing with a cached
    tier.
    """
    points = {}
    for winner, loser, tie in rows:
        points.setdefault(winner, 0.0)
        points.setdefault(loser, 0.0)
        if tie:
            points[winner] += 0.5
            points[loser] += 0.5
        else:
            points[winner] += 1.0

    tiers, current, last = [], [], None
    for item, score in sorted(points.items(), key=lambda kv: (-kv[1], kv[0])):
        if last is not None and score != last:
            tiers.append(current)
            current = []
        current.append(item)
        last = score
    if current:
        tiers.append(current)
    return tiers


# ── set selection ────────────────────────────────────────────────────────────

# ELO-equivalent bonus applied to a title the caller marked as priority. Large
# enough to outrank score-proximity (which spans ~100 ELO within a tier) so a
# needed title beats a merely convenient one, without disabling the tier fence.
PRIORITY_WEIGHT = 250.0


def select_set(candidates, size=6, rng=None, boundary_bias=0.65, priority=None):
    """Choose `size` titles to show together for one ranking round.

    `candidates` is a sequence of dicts carrying `tier`, `elo_score` and
    `elo_sigma` (sigma may be None). Returns the chosen dicts.

    Two competing goals, blended by `boundary_bias`:

      * **Reduce uncertainty** — favour titles whose posterior SD is still wide.
      * **Probe tier boundaries** — pairing by nearest score means "same tier"
        (because a score barely moves off its seed), and in the measured original
        that made **86.8% of all matches intra-tier**: clicks that re-confirm
        what the filed tier already said, while the tier assignments themselves
        went unscrutinised. The information about whether the *tiers* are right
        lives at the boundaries, so a fraction of each set is drawn from the
        neighbouring tier.

    Note the fix is a *bias*, not a rule. Rounding the boundary quota made duels
    100% cross-tier, which is the opposite failure — within-tier ordering stops
    being measured at all. The fractional part is taken as a probability.

    The set is still kept narrow in score terms: a tier-1 against a tier-5 is a
    foregone conclusion and teaches nothing.
    """
    rng = rng or np.random.default_rng()
    pool = list(candidates)
    if len(pool) <= size:
        return pool

    sigmas = np.array([_sigma_of(c) for c in pool], dtype=np.float64)
    scores = np.array([float(c.get('elo_score') or DEFAULT_SEED_ELO) for c in pool])
    filed = np.array([int(c.get('tier') or 0) for c in pool])

    # Caller-supplied preference (1 = wanted, 0 = fine either way). Needed because
    # SIGMA CANNOT DISCRIMINATE on a thin corpus: titles with 0 rounds and titles
    # with 3 differ by ~1 ELO of posterior width, so ranking by sigma alone let
    # already-audited titles take ~43% of slots when they were meant to be a 12%
    # spot-check trickle — the boundary-biased fill hunts exactly where those
    # titles sit, amplifying their share ~4x.
    if priority is None:
        prio = np.zeros(len(pool), dtype=np.float64)
    else:
        prio = np.asarray(priority, dtype=np.float64)

    # Anchor on a wanted / high-uncertainty title (random among the top decile, so
    # consecutive rounds don't keep serving the same card).
    top = np.argsort(-(sigmas + PRIORITY_WEIGHT * prio))[:max(size, len(pool) // 10)]
    anchor = int(rng.choice(top))
    a_score, a_tier = scores[anchor], filed[anchor]

    # Hard eligibility fence: never mix tiers more than one step from the anchor.
    # Enforced structurally rather than left to score proximity, so a very
    # uncertain tier-5 can't be dragged into a tier-2 set where the outcome is a
    # foregone conclusion and the click is wasted.
    if a_tier:
        eligible = np.flatnonzero(np.abs(filed - a_tier) <= 1)
    else:
        eligible = np.arange(len(pool))
    eligible = eligible[eligible != anchor]

    chosen = [anchor]
    raw = (size - 1) * boundary_bias
    n_boundary = int(raw) + (1 if rng.random() < (raw - int(raw)) else 0)

    # Boundary slots: adjacent tier, nearest in score to the anchor.
    if a_tier and n_boundary:
        adj = eligible[np.abs(filed[eligible] - a_tier) == 1]
        if adj.size:
            near = adj[np.argsort(np.abs(scores[adj] - a_score)
                                  - PRIORITY_WEIGHT * prio[adj])]
            shortlist = near[:max(n_boundary * 4, 8)]
            take = min(n_boundary, shortlist.size)
            chosen.extend(int(i) for i in rng.choice(shortlist, size=take, replace=False))

    # Remaining slots: same-ish score, weighted toward unresolved titles. Both
    # terms are on the ELO scale, so the coefficient is O(1); scaling it up would
    # collapse this into a pure sigma sort and let the set drift far apart.
    remaining = size - len(chosen)
    if remaining > 0:
        rest = np.setdiff1d(eligible, np.asarray(chosen, dtype=eligible.dtype))
        if rest.size:
            proximity = np.abs(scores[rest] - a_score)
            order = rest[np.argsort(proximity - 1.5 * sigmas[rest]
                                    - PRIORITY_WEIGHT * prio[rest])]
            shortlist = order[:max(remaining * 4, 10)]
            take = min(remaining, shortlist.size)
            chosen.extend(int(i) for i in rng.choice(shortlist, size=take, replace=False))

    # Backfill from the whole pool if the tier fence left the set short.
    if len(chosen) < size:
        rest = np.setdiff1d(np.arange(len(pool)), np.asarray(chosen))
        order = rest[np.argsort(np.abs(scores[rest] - a_score))]
        chosen.extend(int(i) for i in order[:size - len(chosen)])

    return [pool[i] for i in chosen]


def _sigma_of(candidate):
    s = candidate.get('elo_sigma')
    if s is None:
        return PRIOR_SD * ELO_PER_LOGIT
    return float(s)
