import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from curator import catalog, config, db  # noqa: E402


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """A throwaway database. Never touches the real data/curator.db."""
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(config, 'CACHE_DIR', tmp_path / 'cache')
    monkeypatch.setattr(config, 'POSTER_CACHE', tmp_path / 'cache' / 'posters')
    monkeypatch.setattr(config, 'DB_PATH', tmp_path / 'test.db')
    db.close()
    db.ensure_schema()
    yield db
    db.close()


@pytest.fixture
def make_item(temp_db):
    counter = {'n': 0}

    def _make(media_type, title=None, tier=None):
        counter['n'] += 1
        item_id, _ = catalog.upsert({
            'title': title or f'{media_type} {counter["n"]}',
            'media_type': media_type,
            'tier': tier,
            'year': 2000 + counter['n'],
        })
        return item_id
    return _make
