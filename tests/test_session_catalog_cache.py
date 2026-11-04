import json
import time

import pytest

from claudapter.catalog import _identity, cached_model_names
from claudapter.config import Config, Provider


@pytest.mark.parametrize("change", ["fresh", "stale", "credential", "identity", "bad_name", "invalid", "oversize"])
def test_cache_enrichment_is_read_only_and_never_adds_model_ids(tmp_path, monkeypatch, change):
    from claudapter import catalog

    path = tmp_path / "catalog.json"
    monkeypatch.setattr(catalog, "cache_path", lambda config: path)
    provider = Provider("p", "P", "https://fixture.test/v1")
    config = Config(providers={"p": provider}, path=tmp_path / "config.toml")
    entry = {"identity": _identity(config, provider, token="synthetic"), "saved_at": time.time(),
             "models": [{"id": "a", "display_name": "Friendly"}, {"id": "b", "display_name": "Other"}]}
    if change == "stale":
        entry["saved_at"] = 0
    if change == "identity":
        entry["identity"]["base_url"] = "https://other.test/v1"
    if change == "bad_name":
        entry["models"][0]["display_name"] = "bad\nname"
    raw = "invalid" if change == "invalid" else " " * (8 * 1024 * 1024 + 1) if change == "oversize" else json.dumps({"p": entry})
    path.write_text(raw, encoding="utf-8")
    result = cached_model_names(config, provider, ("a",), token="changed" if change == "credential" else "synthetic")
    assert result == ({"a": "Friendly"} if change == "fresh" else {})
    assert path.read_text(encoding="utf-8") == raw and not config.path.exists()
