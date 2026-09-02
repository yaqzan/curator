"""The Obsidian vault's media log — a second watch ledger, older than Plex.

`Recommendations/Reading List/Media` is one note per thing consumed, exported out
of Notion, with the interesting facts in the frontmatter:

    ---
    Type: "Film"
    Status: "Finished"
    Epoch: "Spring 2025"
    Started: 2025-03-29
    Finished: 2025-03-29
    Score /5: "⭐️⭐️⭐️⭐️⭐️"
    Hours: 2.25
    ---

**The filename IS the title**, and it is the only handle we get. There is no
GUID, no year and no external id, so every entry has to be resolved against Plex
Discover by name; see `catalog.import_obsidian` for how a match is accepted or
refused.

> Nineteen notes do carry a `Title:` field, and it is **not** the better source —
> twelve of them say "Untitled" and the rest either repeat the filename or add
> back punctuation the exporter stripped. Don't switch to it.

Why this matters more than it looks: the vault covers a decade of cinema watched
away from Plex, and **every film entry carries a star rating**, which is the tier
seed the whole audit hangs off. The Plex import could seed only 6 tiers out of
148 titles; this log carries 205 judgements on its own.

This module only reads. It never writes into the vault.
"""

from __future__ import annotations

import datetime
import json
import re
import unicodedata
from pathlib import Path

from .. import media_types

# The vault's own vocabulary -> our contests. Everything absent is deliberate:
# games, books, audiobooks, manga and decks are consumed media the vault tracks
# and Curator has no contest for. An unmapped type is skipped, never guessed at.
TYPE_MAP = {
    'film': media_types.MOVIE.key,
    'tv': media_types.TV.key,
    'anime': media_types.ANIME.key,
    'anime movie': media_types.ANIME_MOVIE.key,
    'documentary': media_types.DOCUMENTARY.key,
}

DEFAULT_VAULT = Path(r'C:\Obsidian\Mycelium\Recommendations\Reading List\Media')
DEFAULT_OVERRIDES = Path(__file__).resolve().parents[2] / 'ops' / 'obsidian-overrides.json'

_SEASONS = {'winter': 1, 'spring': 4, 'summer': 7, 'fall': 10, 'autumn': 10}


def tier_from_stars(text):
    """"⭐️⭐️⭐️⭐️" -> 4. Anything without stars has no tier.

    The variation selector (U+FE0F) after each star is why this counts the star
    codepoint rather than the string length.
    """
    if not text:
        return None
    count = str(text).count('⭐')
    return min(5, count) or None


def _watch_year(entry):
    """The latest year the note says it was consumed, or None.

    Used to fence remakes: a film watched in 2019 is not the 2024 version. The
    `Epoch` ("Fall 2025", sometimes a comma-separated list of re-watches) is the
    only date most older entries carry.
    """
    years = []
    for key in ('Finished', 'Started'):
        match = re.search(r'(\d{4})', str(entry.get(key) or ''))
        if match:
            years.append(int(match.group(1)))
    for match in re.finditer(r'(\d{4})', str(entry.get('Epoch') or '')):
        years.append(int(match.group(1)))
    return max(years) if years else None


def _watched_at(entry):
    """`Finished`/`Started` as a unix timestamp, else the middle of the Epoch."""
    for key in ('Finished', 'Started'):
        raw = str(entry.get(key) or '').strip()
        try:
            return int(datetime.datetime.strptime(raw[:10], '%Y-%m-%d').timestamp())
        except (ValueError, OSError):
            continue
    epoch = str(entry.get('Epoch') or '').split(',')[0].strip().lower()
    match = re.match(r'(winter|spring|summer|fall|autumn)\s+(\d{4})', epoch)
    if match:
        try:
            return int(datetime.datetime(
                int(match.group(2)), _SEASONS[match.group(1)], 15).timestamp())
        except (ValueError, OSError):
            return None
    return None


def _parse_frontmatter(text):
    """The `key: value` block between the leading `---` fences.

    Hand-parsed rather than pulled through a YAML library: the exporter writes
    flat scalars only, and this is not worth a dependency. A file without a
    frontmatter block yields nothing.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---':
        return {}
    fields = {}
    for line in lines[1:]:
        if line.strip() == '---':
            break
        key, sep, value = line.partition(':')
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        fields[key.strip()] = value
    return fields


def normalize_title(title):
    """Fold a title to something two spellings of it agree on.

    The Notion export stripped colons and swapped hyphens for en-dashes, so
    "Star Wars Episode V – The Empire Strikes Back" has to meet
    "Star Wars: Episode V - The Empire Strikes Back" somewhere. Accents, case,
    punctuation and `&`/`and` all fold away; word order does not.
    """
    text = unicodedata.normalize('NFKD', str(title or ''))
    text = ''.join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace('&', ' and ')
    text = re.sub(r'[^a-z0-9]+', ' ', text)
    return ' '.join(text.split())


def clean_title(name):
    """Filename -> `(title, asserted_year)`.

    Two artefacts live in these names. A few carry the year the user added to
    tell two films apart — `Blade Runner (1982)`, `Persona (1966)` — which is the
    strongest signal in the whole import and has to come out of the string before
    it is searched, because Discover ranks `Blade Runner (1982)` *below*
    `Blade Runner 2049`. The rest is a `Media - ` prefix the Notion export left on
    a handful of notes.
    """
    title = re.sub(r'^\s*Media\s*[-–—]\s*', '', str(name or '')).strip()
    year = None
    match = re.search(r'\s*\((\d{4})\)\s*$', title)
    if match:
        year = int(match.group(1))
        title = title[:match.start()].strip()
    return title, year


# `Attack on Titan S01 (rewatch)`, `Black Mirror S04`, `Attack on Titan The Final
# Season Part 3`. Anchored at the end and applied repeatedly, so a title that
# merely contains a number (`Mob Psycho 100`, `Kaiji 2 Against All Rules`) is
# untouched — only a trailing marker counts.
_SEASON_SUFFIX = re.compile(
    r'(?i)(?:\s*[(\[]?\s*re-?watch\s*[)\]]?)\s*$'
    r'|\s+(?:the\s+)?final\s+season(?:\s+part\s*\d+)?\s*$'
    r'|\s+(?:s\d{1,2}|season\s*\d+|part\s*\d+)\s*$')


def series_title(name):
    """`Black Mirror S04` -> `Black Mirror`. Applied until it stops changing.

    The dangling-separator strip is why `Breaking Bad S05 -` resolves: the note
    names carry the leftovers of a subtitle the export dropped.
    """
    title, previous = str(name or '').strip(), None
    while previous != title:
        previous = title
        title = _SEASON_SUFFIX.sub('', title).strip()
        title = re.sub(r'[\s\-–—:,]+$', '', title).strip()
    return title or str(name or '').strip()


def read_overrides(path=None):
    """The hand-written answers for notes the resolver cannot settle alone.

    A JSON object keyed by the note's filename, each value any of:

        "Dune":        {"title": "Dune: Part One", "year": 2021}
        "Midsommer":   {"title": "Midsommar", "year": 2019}
        "House (1977)": {"query": "Hausu", "title": "House", "year": 1977}
        "The End of the Fucking World": {"type": "tv"}
        "Her":         {"manual": true, "year": 2013, "why": "absent from Discover"}

    `title` is the title *Plex* uses — it becomes both the search and the string
    the match is made against. `query` overrides only the search, for the case
    where the two differ: Discover finds Obayashi's *House* under "Hausu" and
    under nothing else, but the record it returns is titled "House".
    `year` is asserted outright, `guid` bypasses search entirely, and `type`
    re-files a note the vault put in the wrong bucket. Because the lookup runs
    *before* season notes are grouped, an override's `title` also merges: it is
    what folds `Demon Slayer - Hashira Training Arc` into its parent series.

    `manual` builds the row from the override itself, with no Plex record behind
    it. Discover's index simply does not contain Jonze's *Her*, Ti West's *Pearl*
    or Fincher's *The Game* — no query reaches them — and a watched film with a
    real rating belongs in its contest whether or not a metadata provider agrees
    it exists. Such a row carries no poster and no external ids, so it matches on
    title+year alone; keep the year accurate or a later import will duplicate it.

    This exists because roughly a third of a decade-old media log cannot be
    resolved from a bare name: `Dune` watched in 2021 is not Lynch's, `Her` is
    not in Discover's first twenty hits for "her", and `Midsommer` is a typo.
    Keeping the answers in a file rather than in one person's head is what makes
    the import reproducible — re-running it after a vault edit must not require
    re-deciding all of this.
    """
    file = Path(path or DEFAULT_OVERRIDES)
    if not file.exists():
        return {}
    return json.loads(file.read_text(encoding='utf-8'))


def read_vault(path=None, media_type=None, status='Finished', overrides=None):
    """Every note in the vault folder, as import-ready entries.

    Returns `(entries, report)`. `entries` are the ones that map onto a contest;
    the report counts what was skipped and why, because "802 notes in, 205 out"
    is otherwise indistinguishable from a parser that quietly broke.
    """
    folder = Path(path or DEFAULT_VAULT)
    if not folder.is_dir():
        raise FileNotFoundError(f'No Obsidian media folder at {folder}')

    overrides = {} if overrides is None else overrides
    wanted = media_types.get(media_type).key if media_type else None
    groups, report = {}, {
        'notes': 0, 'no_frontmatter': 0, 'unmapped_type': 0, 'wrong_status': 0,
        'filtered_out': 0, 'by_type': {}, 'untiered': 0, 'overridden': 0,
        'skipped': [], 'merged': 0,
    }

    for note in sorted(folder.glob('*.md')):
        report['notes'] += 1
        fields = _parse_frontmatter(note.read_text(encoding='utf-8'))
        if not fields:
            report['no_frontmatter'] += 1
            continue

        # `Black Mirror S01..S06` is SIX notes about ONE thing this app ranks:
        # the TV contest judges "the show as a whole, not one season". Rewatches
        # collapse the same way. Films keep their own names — `Berserk Golden Age
        # Arc I/II/III` are three films, not three seasons.
        title, asserted_year = clean_title(note.stem)
        name = series_title(title)

        # Resolved BEFORE grouping, so an override's title is also what the
        # grouping keys on: that is what folds `Demon Slayer - Hashira Training
        # Arc` into the series it is a season of, and averages their ratings.
        override = overrides.get(note.stem) or overrides.get(name) or {}
        key = TYPE_MAP.get((override.get('type')
                            or fields.get('Type') or '').strip().lower())
        if not key:
            report['unmapped_type'] += 1
            continue
        if status and (fields.get('Status') or '').strip().lower() != status.lower():
            report['wrong_status'] += 1
            continue
        if wanted and key != wanted:
            report['filtered_out'] += 1
            continue
        if override.get('skip'):
            report['skipped'].append({'title': note.stem, 'why': override['skip']})
            continue

        tier = tier_from_stars(fields.get('Score /5'))
        if not tier:
            report['untiered'] += 1
        report['by_type'][key] = report['by_type'].get(key, 0) + 1

        name = override.get('title') or name
        group = groups.setdefault((key, name.lower()), {
            'title': name, 'notes': [], 'tiers': [], 'media_type': key,
            'asserted_year': override.get('year') or asserted_year,
            'watch_year': None, 'watched_at': None, 'epoch': fields.get('Epoch'),
            'override': override,
        })
        group['notes'].append(note.stem)
        if override:
            group['override'] = override
            report['overridden'] += 1
        if tier:
            group['tiers'].append(tier)
        group['asserted_year'] = group['asserted_year'] or asserted_year
        for field, value in (('watch_year', _watch_year(fields)),
                             ('watched_at', _watched_at(fields))):
            if value and value > (group[field] or 0):
                group[field] = value
                if field == 'watch_year':
                    group['epoch'] = fields.get('Epoch')

    entries = []
    for group in groups.values():
        if len(group['notes']) > 1:
            report['merged'] += len(group['notes']) - 1
        # One tier for a show whose seasons were rated separately: the mean,
        # rounded up at the half. The alternative — the best season — would rank
        # a show on its peak, which is not what "order these shows" asks.
        tier = (int(sum(group['tiers']) / len(group['tiers']) + 0.5)
                if group['tiers'] else None)
        override = group['override']

        entries.append({
            'title': group['title'],
            'note': group['title'],
            'notes': group['notes'],
            'match': override.get('title') or group['title'],
            'query': override.get('query') or override.get('title') or group['title'],
            'guid': override.get('guid'),
            'manual': bool(override.get('manual')),
            'asserted_year': override.get('year') or group['asserted_year'],
            'media_type': group['media_type'],
            'tier': tier,
            'watch_year': group['watch_year'],
            'watched_at': group['watched_at'],
            'epoch': group['epoch'],
        })

    entries.sort(key=lambda e: (e['media_type'], e['title'].lower()))
    return entries, report
