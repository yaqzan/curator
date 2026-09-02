"""The Obsidian media log: parsing it, and resolving its titles against Plex.

The thing being defended here is that **a note only ever becomes a catalog row
when the film is actually pinned down**. A vault note carries a real star rating,
so a wrong match does not just add a wrong title — it files a human judgement
against it, and nothing downstream can tell that the judgement is about a
different film.
"""

from curator import catalog
from curator.sources import obsidian


def write_note(folder, name, **fields):
    lines = ['---'] + [f'{k.replace("_", " ")}: "{v}"' for k, v in fields.items()] + ['---']
    (folder / f'{name}.md').write_text('\n'.join(lines), encoding='utf-8')


def hit(title, year, kind='movie'):
    return {'title': title, 'year': year, 'type': kind,
            'guid': f'plex://movie/{title}-{year}', 'rating_key': str(year)}


# ── reading the vault ────────────────────────────────────────────────────────

def test_stars_become_tiers():
    # The exporter writes each star with a U+FE0F variation selector after it,
    # so the string is twice as long as it looks.
    assert obsidian.tier_from_stars('⭐️⭐️⭐️⭐️') == 4
    assert obsidian.tier_from_stars('⭐️') == 1
    assert obsidian.tier_from_stars('') is None


def test_only_watchable_types_and_finished_entries_are_imported(tmp_path):
    write_note(tmp_path, 'Anora', Type='Film', Status='Finished')
    write_note(tmp_path, 'A Short Hike', Type='Xbox Series X', Status='Finished')
    write_note(tmp_path, 'Dune Messiah', Type='Book', Status='Finished')
    write_note(tmp_path, 'Half Watched', Type='Film', Status='Watching')

    entries, report = obsidian.read_vault(tmp_path)

    assert [e['title'] for e in entries] == ['Anora']
    assert report['unmapped_type'] == 2      # the game and the book
    assert report['wrong_status'] == 1


def test_a_year_in_the_note_name_is_an_assertion_not_part_of_the_title(tmp_path):
    # `Blade Runner (1982)` must be SEARCHED as "Blade Runner" — Discover ranks
    # the parenthesised form below Blade Runner 2049 — and then pinned to 1982.
    write_note(tmp_path, 'Blade Runner (1982)', Type='Film', Status='Finished')
    entries, _ = obsidian.read_vault(tmp_path)
    assert entries[0]['title'] == 'Blade Runner'
    assert entries[0]['asserted_year'] == 1982


def test_the_epoch_dates_an_entry_that_has_no_watch_dates(tmp_path):
    write_note(tmp_path, 'Akira', Type='Film', Status='Finished', Epoch='Summer 2019')
    entries, _ = obsidian.read_vault(tmp_path)
    assert entries[0]['watch_year'] == 2019


def test_normalize_folds_the_punctuation_the_export_mangled():
    # The Notion export dropped colons and swapped hyphens for en-dashes.
    assert (obsidian.normalize_title('Star Wars Episode V – The Empire Strikes Back')
            == obsidian.normalize_title('Star Wars: Episode V - The Empire Strikes Back'))
    assert obsidian.normalize_title('La Haine') == obsidian.normalize_title('La haine')


# ── seasons collapse into one series ─────────────────────────────────────────

def test_season_notes_become_one_entry_scored_by_their_mean(tmp_path):
    """The TV contest judges "the show as a whole, not one season", so six
    `Black Mirror S0n` notes are six ratings of ONE thing to rank. The mean is
    used rather than the best season, which would rank a show on its peak."""
    for season, stars in (('S01', 3), ('S02', 4), ('S03', 5)):
        write_note(tmp_path, f'Black Mirror {season}', Type='TV', Status='Finished',
                   Epoch='Fall 2020', **{'Score /5': '⭐️' * stars})
    write_note(tmp_path, 'Black Mirror S01 (rewatch)', Type='TV', Status='Finished',
               Epoch='Fall 2021', **{'Score /5': '⭐️⭐️⭐️⭐️'})

    entries, report = obsidian.read_vault(tmp_path)

    assert len(entries) == 1
    assert entries[0]['title'] == 'Black Mirror'
    assert entries[0]['tier'] == 4          # (3+4+5+4)/4 = 4.0
    assert entries[0]['watch_year'] == 2021  # the latest watch of any season
    assert report['merged'] == 3


def test_a_number_in_a_title_is_not_a_season_marker():
    # Only a trailing marker counts, or `Kaiji 2 Against All Rules` and
    # `Mob Psycho 100` would both lose their tails.
    assert obsidian.series_title('Kaiji 2 Against All Rules') == 'Kaiji 2 Against All Rules'
    assert obsidian.series_title('Mob Psycho 100') == 'Mob Psycho 100'
    assert obsidian.series_title('Attack on Titan The Final Season Part 3') == 'Attack on Titan'
    assert obsidian.series_title('Breaking Bad S05 -') == 'Breaking Bad'


def test_films_are_never_collapsed_into_each_other(tmp_path):
    # Three films of one arc, not three seasons of one show.
    for numeral in ('I - The Egg', 'II - The Battle for Doldrey', 'III - Descent'):
        write_note(tmp_path, f'Berserk Golden Age Arc {numeral}',
                   Type='Anime Movie', Status='Finished', **{'Score /5': '⭐️⭐️⭐️⭐️'})
    entries, _ = obsidian.read_vault(tmp_path)
    assert len(entries) == 3


def test_an_override_title_is_what_the_grouping_keys_on(tmp_path):
    """`Demon Slayer - Hashira Training Arc` is a season of the series, and no
    rule can know that — but the override that names the series must then fold
    the two notes together rather than filing the arc as a second anime."""
    write_note(tmp_path, 'Demon Slayer', Type='Anime', Status='Finished',
               **{'Score /5': '⭐️⭐️⭐️'})
    write_note(tmp_path, 'Demon Slayer - Hashira Training Arc', Type='Anime',
               Status='Finished', **{'Score /5': '⭐️⭐️⭐️⭐️⭐️'})

    entries, _ = obsidian.read_vault(tmp_path, overrides={
        'Demon Slayer': {'title': 'Demon Slayer: Kimetsu no Yaiba', 'year': 2019},
        'Demon Slayer - Hashira Training Arc': {'title': 'Demon Slayer: Kimetsu no Yaiba',
                                                'year': 2019},
    })

    assert len(entries) == 1
    assert entries[0]['title'] == 'Demon Slayer: Kimetsu no Yaiba'
    assert entries[0]['tier'] == 4          # (3+5)/2


def test_a_manual_override_builds_a_row_with_no_plex_record_behind_it(temp_db):
    """Discover's index has no Jonze's *Her* and no query reaches it. A watched,
    rated film belongs in its contest whether or not a provider agrees it
    exists — and title+year is all such a row has to match on next time."""
    result = catalog.import_obsidian([{
        'title': 'Her', 'note': 'Her', 'match': 'Her', 'query': 'Her', 'notes': ['Her'],
        'manual': True, 'guid': None, 'asserted_year': 2013, 'media_type': 'movie',
        'tier': 3, 'watch_year': 2024, 'watched_at': None, 'epoch': 'Winter 2024',
    }])

    assert result['created'] == 1 and result['manual'] == ['Her (2013)']
    row = temp_db.one('SELECT * FROM items WHERE title = ?', ('Her',))
    assert (row['year'], row['tier'], row['watched']) == (2013, 3, 1)
    assert row['plex_guid'] is None


def test_an_override_can_move_a_note_to_the_contest_it_belongs_in(tmp_path):
    write_note(tmp_path, 'The End of the Fucking World', Type='Film', Status='Finished',
               **{'Score /5': '⭐️⭐️⭐️⭐️'})
    entries, _ = obsidian.read_vault(tmp_path, overrides={
        'The End of the Fucking World': {'type': 'tv'}})
    assert entries[0]['media_type'] == 'tv'


# ── resolving a title against Plex ───────────────────────────────────────────

def entry(title, **kwargs):
    base = {'title': title, 'note': title, 'query': title, 'match': title,
            'media_type': 'movie', 'tier': 4, 'asserted_year': None,
            'watch_year': None, 'guid': None}
    base.update(kwargs)
    return base


def test_one_film_of_that_name_resolves():
    found, why, _ = catalog.resolve_obsidian_entry(
        entry('Anora'), search=lambda q, limit: [hit('Anora', 2024)])
    assert found['year'] == 2024 and why == 'matched'


def test_two_films_of_that_name_resolve_to_NOTHING():
    """The whole point. `Pearl` names six films; Discover's top hit is not the
    famous one, and a plausible wrong answer here is worse than no answer."""
    found, why, candidates = catalog.resolve_obsidian_entry(
        entry('Pearl'), search=lambda q, limit: [hit('Pearl', 2009), hit('Pearl', 2022)])
    assert found is None and why == 'ambiguous'
    assert len(candidates) == 2          # both handed back for a human


def test_a_duplicate_record_is_not_an_ambiguity():
    # Discover's index carries the same film twice, and a year-less shadow of
    # half of everything. Neither is a second film.
    found, why, _ = catalog.resolve_obsidian_entry(
        entry('Fight Club'),
        search=lambda q, limit: [hit('Fight Club', 1999), hit('Fight Club', 1999),
                                 hit('Fight Club', None)])
    assert found['year'] == 1999 and why == 'matched'


def test_the_watch_date_fences_out_a_release_that_did_not_exist_yet():
    found, why, _ = catalog.resolve_obsidian_entry(
        entry('Suspiria', watch_year=1990),
        search=lambda q, limit: [hit('Suspiria', 2018), hit('Suspiria', 1977)])
    assert found['year'] == 1977 and why == 'matched'


def test_an_asserted_year_settles_it_outright():
    found, why, _ = catalog.resolve_obsidian_entry(
        entry('Carrie', asserted_year=1976, watch_year=2024),
        search=lambda q, limit: [hit('Carrie', 2013), hit('Carrie', 1976)])
    assert found['year'] == 1976 and why == 'matched-year'


def test_a_series_is_never_accepted_for_a_film_note():
    found, why, _ = catalog.resolve_obsidian_entry(
        entry('The End of the Fucking World'),
        search=lambda q, limit: [hit('The End of the Fucking World', 2017, 'show')])
    assert found is None and why == 'wrong-kind'


def test_an_override_can_search_under_one_name_and_match_another():
    # Discover finds Obayashi's House only under "Hausu", and returns "House".
    found, why, _ = catalog.resolve_obsidian_entry(
        entry('House', query='Hausu', match='House', asserted_year=1977),
        search=lambda q, limit: [hit('House', 1977)] if q == 'Hausu' else [])
    assert found['year'] == 1977 and why == 'matched-year'


# ── what an import is allowed to change ──────────────────────────────────────

def test_a_star_rating_fills_a_blank_tier_but_never_replaces_a_filed_one(temp_db):
    """`_REFRESHABLE` keeps `tier` out of an import's reach; the one exception is
    a tier that is NULL, which is a gap rather than a judgement. This is what
    let 205 vault ratings seed a catalog that had 6 tiers filed."""
    blank, _ = catalog.upsert({'title': 'Anora', 'year': 2024, 'media_type': 'movie',
                               'tmdb_id': '1064213'})
    filed, _ = catalog.upsert({'title': 'Memento', 'year': 2000, 'media_type': 'movie',
                               'tmdb_id': '77', 'tier': 2})

    catalog.upsert({'title': 'Anora', 'year': 2024, 'media_type': 'movie',
                    'tmdb_id': '1064213', 'tier': 5}, source='obsidian')
    catalog.upsert({'title': 'Memento', 'year': 2000, 'media_type': 'movie',
                    'tmdb_id': '77', 'tier': 5}, source='obsidian')

    assert temp_db.one('SELECT tier FROM items WHERE id = ?', (blank,))['tier'] == 5
    assert temp_db.one('SELECT tier FROM items WHERE id = ?', (filed,))['tier'] == 2


def test_a_vault_entry_updates_the_plex_row_rather_than_duplicating_it(temp_db):
    """Both sources describe the same film. Two rows would split its comparison
    history in half — `find_existing` matching on the external id prevents it."""
    plex_row, _ = catalog.upsert({'title': 'Anora', 'year': 2024, 'media_type': 'movie',
                                  'tmdb_id': '1064213'}, source='plex-watch')
    same, action = catalog.upsert({'title': 'Anora', 'year': 2024, 'media_type': 'movie',
                                   'tmdb_id': '1064213', 'tier': 5}, source='obsidian')
    assert (same, action) == (plex_row, 'updated')
    assert temp_db.one('SELECT COUNT(*) n FROM items')['n'] == 1
