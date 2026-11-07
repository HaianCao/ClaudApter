import pytest


@pytest.fixture(autouse=True)
def _isolated_catalog_cache(tmp_path, monkeypatch):
    """Launch code reads the model catalog cache; never let tests touch the real one."""
    monkeypatch.setenv("CLAUDE_CA_CACHE", str(tmp_path / "isolated-catalog-cache.json"))
