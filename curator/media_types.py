"""The contests we run, and everything that differs between them.

Each media type is a **separate ranking contest that shares one scorer and one
table**. "Is Attack on Titan better than The Sopranos?" is not a question this
app asks — the whole point of splitting by type is that a 9/10 anime and a 9/10
prestige drama are not competing for the same slot in your head.

**This module is the ONLY place a type key, label or piece of surface copy is
written down (don't hardcode 'movie' anywhere else).** Every scoring read of
`matches` must filter on `media_type`; a query that forgets silently scores one
contest with another's judgements, and because every contest lives on the same
800-1200 scale seeded off a 1-5 tier, nothing about the output looks wrong.

Why one table with a discriminator rather than one table per type: the scorer,
the Plackett-Luce decode, undo, the review queue and the correction UI are all
identical and none of them care what is being judged. N parallel tables would
mean N copies of each. The cost is a `WHERE` on every query — cheap, and indexed.

Adding a type is a one-entry change here plus a `_seed_types` run; nothing else
needs to know.
"""

from __future__ import annotations

from collections import namedtuple

MediaType = namedtuple('MediaType', [
    'key',        # stored in items.media_type / matches.media_type
    'label',      # "Movies" — the plural noun, for tabs and headings
    'singular',   # "movie" — for sentences
    'icon',       # emoji shown on the tab
    'question',   # what the user is being asked, verbatim, in the UI
    'subject',    # what is on the card, for tooltips and empty states
    'order',      # display order in the tab strip
])

MOVIE = MediaType(
    key='movie',
    label='Movies',
    singular='movie',
    icon='🎬',
    question='Order these films — best first.',
    subject='the film as a whole',
    order=10,
)

TV = MediaType(
    key='tv',
    label='TV',
    singular='series',
    icon='📺',
    question='Order these shows — best first.',
    subject='the show as a whole, not one season',
    order=20,
)

ANIME = MediaType(
    key='anime',
    label='Anime',
    singular='anime series',
    icon='🌸',
    question='Order these anime — best first.',
    subject='the series as a whole',
    order=30,
)

ANIME_MOVIE = MediaType(
    key='anime_movie',
    label='Anime Films',
    singular='anime film',
    icon='🎴',
    question='Order these anime films — best first.',
    subject='the film as a whole',
    order=40,
)

DOCUMENTARY = MediaType(
    key='documentary',
    label='Docs',
    singular='documentary',
    icon='🎥',
    question='Order these documentaries — best first.',
    subject='the documentary as a whole',
    order=50,
)

TYPES = {t.key: t for t in (MOVIE, TV, ANIME, ANIME_MOVIE, DOCUMENTARY)}
ORDERED = sorted(TYPES.values(), key=lambda t: t.order)
DEFAULT = MOVIE

# The 1-5 tier ladder every contest shares. It is deliberately shared: each type's
# tier column is the same kind of scale ("how good, in five steps"), so the seeds,
# the prior width and the audit thresholds all transfer. If that ever stops being
# true for one type, the per-type knobs move onto the descriptor above rather than
# branching inside the scorer.
TIER_LABELS = {
    1: 'Bad',
    2: 'Weak',
    3: 'Good',
    4: 'Great',
    5: 'All-time',
}


def get(key):
    """Resolve a type key. Unknown/blank falls back to movies.

    Deliberately forgiving rather than 400-ing: an omitted `?type=` should behave
    like the default tab, not break the page.
    """
    if not key:
        return DEFAULT
    return TYPES.get(str(key).strip().lower(), DEFAULT)


def is_known(key):
    return str(key or '').strip().lower() in TYPES


def as_dicts():
    """The registry, in the shape the frontend mirrors for rendering copy."""
    return [t._asdict() for t in ORDERED]
