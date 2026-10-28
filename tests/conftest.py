import pytest

from bridge.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(db_path=tmp_path / "t.db")
