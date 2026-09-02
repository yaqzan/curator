# Ranking & Scoring Design — don't silently revert

Load-bearing decisions behind Curator's contest/scoring model. Port of Archivist's
`archiver/local_tube/face_rank.py`, measured against a real corpus there —
`C:\Development\Archivist\.claude\docs\face-rank-elo.md` is the long form.

## Every media type is a separate contest, every scoring read filters on it

`items.media_type` is the contest; `matches.media_type` carries it too, and
`ranking.load_observations` filters on it. "Is Attack on Titan better than The
Sopranos?" is not a question this app asks.

**Silent failure mode:** every contest sits on the same 800-1200 scale seeded off
the same 1-5 tier ladder, so a query that forgets its filter returns plausible
numbers — no crash, no NULL. `tests/ranking_test.py::test_a_round_in_one_contest_does_not_move_another`
is the only thing that catches it.

`ranking.record_ranking` **rejects** a round mixing types rather than recording
it. Re-filing a title drops its comparison history (belonged to the old contest).

## The scorer is a batch fit, not online ELO

Every score is refit from the entire history after every submission. Simulation
put online ELO, Glicko-style adaptive-K and Bradley-Terry within 0.01 Spearman of
each other — the batch fit is for structural properties, not accuracy:

1. Order independence (online ELO's answer depends on arrival order).
2. Transitive propagation (A beats B, B beats C -> C drops immediately).
3. A real posterior SD, which the calibration gate runs on.

## A round is stored as every pair, never scored as independent duels

`record_ranking` writes C(n,2) rows sharing one `set_id`, `is_tie` on same-tier
pairs. `load_observations` rebuilds the weak ordering (`scorer.tiers_from_pairs`)
and decomposes it into Plackett-Luce terms (`scorer.decompose_ranking`) before the
fit sees it.

Scoring the stored pairs as C(n,2) independent duels inflates one judgement by
**1.5x at n=4, 2.0x at n=6, 2.7x at n=9** (measured here; 1.6/2.1/2.9 on the
original corpus). Point estimates survive it; `elo_sigma` does not — every title
would read as settled on half the evidence actually required.
`test_all_pairs_shortcut_would_overstate_the_evidence` pins it, measuring
**information**, not raw sigma (after one round the prior still dominates
posterior width, which is what makes this invisible).

Storing pairs rather than a tier column keeps them the single source of truth, so
a History-tab correction flows straight back into scoring.

## Ordering six, not picking a winner from two

Total Fisher information at equal scores: duel 0.500, top-1 of 6 0.833, **full
ranking of 6 3.550** (7.1x), ranking of 9 6.171. Ordering 6 reached in ~110 rounds
what top-1 needed ~700 for.

Ties are load-bearing: forcing a choice between two titles read as equal feeds a
coin flip into the model as signal.

Grouping applies to the clicks you make, NOT the previously placed title:
`click A -> [A]`, `group B -> [A][B]`, `group C -> [A][B C]`. An earlier version
joined a grouped click to whatever was placed last, making "these next two are
tied" impossible without first mis-tying one.

## Two ways to place, and both must keep working

Click places at the **end** of the order (fast path). Drag places at a **chosen**
position, including back into an order already under way. A drag onto the middle
of a rail row ties with it; onto its top/bottom edge ranks above/below; a placed
chip dragged back to the pool comes out again. `RankPage.applyDrop` does the tier
arithmetic, `dnd.ts` the pointer mechanics.

**Draw the drop indicator ON the row; never insert a placeholder into the list.**
A gap opening under the pointer shifts the row out from under it, so the resolved
target oscillates and the line flickers. `.rail-row.drop-above/.drop-below/.drop-tie`
are pseudo-elements over stable layout; `resolveDrop` reads geometry from
`getBoundingClientRect`.

**A drag closes any open run of tie-clicks.** `groupOpen` means "the tier at the
END of the order is still taking members"; after an arbitrary-position drop that
is no longer the tier the user last touched.

Drag is mouse and pen only, deliberately — claiming touch from the browser's
scroller needs `touch-action: none`, which kills scrolling on the elements that
fill a phone screen; tap-to-place already covers touch. HTML5
`dragstart`/`dragover` isn't used: it can't express "onto = tie, between = rank",
which needs the live pointer against a row's own rectangle.

## Submission is an event consequence, never a render effect

`RankPage.place()` and the drop handler both call `commit()` directly, guarded by
a synchronous `submitLock` ref released only after the *next* set has landed.
`commit` re-checks the lock rather than trusting the top-of-handler check, because
it runs inside a state updater and StrictMode re-runs those in dev.

An earlier version auto-submitted from a `useEffect` watching "all placed" —
because fetching the next set awaits the network, there was a window where the
finished ranking was still in state and the next set hadn't arrived, so the effect
fired again and **every round was recorded twice**.

## PRIOR_SD = 0.45 — don't tighten it

A prior asserting "the filed tiers are already right" makes it nearly impossible
for evidence to prove one wrong. Detection of a title filed one tier too high,
over 4 rounds: 19% at 0.30, **51% at 0.45**, 53% at 0.60. `contested` is a review
queue a human looks at, so recall beats precision.

## Three progress signals — don't collapse them

- **`covered`** — at least `AUDIT_ROUNDS_TARGET` (4) rounds. Coverage, not a claim
  about precision.
- **`contested` / `contested_confident`** — score has left the filed tier. The
  audit's actual finding and the stopping signal.
- **`settled`** — a full sigma clear of the nearest boundary. **Never drive a
  progress bar with this** — rounds needed by distance from a boundary
  (50 ELO -> 1.6, 40 -> 13, 20 -> 109, 10 -> 491) means it never fills for every
  title, implying permanent failure when the work is done.

## Pair selection deliberately crosses tier boundaries

`scorer.select_set` anchors on a high-uncertainty/wanted title and pulls a
`boundary_bias` (0.65) share from the adjacent tier, fenced at +/-1 tier. Sorting
by nearest score alone made **86.8% of all matches intra-tier** in the original —
re-confirming the tier already claimed while it went unscrutinised.

The fix is a bias, not a rule: rounding the quota made duels 100% cross-tier,
which stops within-tier ordering being measured at all — the fractional part is
taken as a probability.

`audit_pool` **must pass `priority`** — don't drop it. On a thin corpus sigma
can't discriminate (0 rounds vs 3 rounds differ by ~1 ELO of posterior width), so
ranking by sigma alone let already-audited titles take ~43% of slots meant to be a
12% trickle.

## An import fills blanks; it never overwrites a judgement

`catalog._REFRESHABLE` lists what a re-import may rewrite. Absent on purpose:
`tier`, `media_type`, `notes`, `archived`. Watch counts only move up. Exception: an
import may set a tier that is NULL (an untiered row is a gap, not a judgement) —
how an Obsidian star rating reaches a Plex-imported-blank title, without touching
a tier filed by hand. Matching is GUID -> external id -> title+year, so a
search-added title later imported doesn't become two rows with split history.
