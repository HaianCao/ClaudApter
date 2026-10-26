import io
from http.client import IncompleteRead
from types import SimpleNamespace

import pytest

from claudapter import catalog
from claudapter.config import Config, Provider
from claudapter.errors import ClaudapterError

INVALID = [b'{"x":' + b'1' * 5000 + b'}',
           b'{"x":' + b'[' * 10000 + b'0' + b']' * 10000 + b'}',
           b'{"x":"\\ud800"}', b'{"x":NaN}', b'{"x":1e309}', b'{', b'\xff']


def config_and_provider(tmp_path, monkeypatch):
    config = Config(path=tmp_path / "settings.toml")
    provider = Provider("fixture", "Fixture", "https://example.invalid/v1")
    monkeypatch.setattr(catalog, "get_token", lambda *args, **kwargs: "")
    return config, provider


@pytest.mark.parametrize("raw", INVALID)
def test_bad_cache_falls_back_without_parser_exception(tmp_path, monkeypatch, raw):
    config, provider = config_and_provider(tmp_path, monkeypatch)
    path = tmp_path / "cache.json"
    path.write_bytes(raw)
    monkeypatch.setattr(catalog, "cache_path", lambda *args: path)
    assert catalog.cached_models(config, provider) == ([], True)


@pytest.mark.parametrize("raw", INVALID)
def test_bad_provider_json_is_typed_and_valid_retry_recovers(tmp_path, monkeypatch, raw):
    _config, _provider = config_and_provider(tmp_path, monkeypatch)
    payloads = iter([raw, b'{"data":[{"id":"fixture-model"}]}'])

    def upstream(*args, **kwargs):
        response = io.BytesIO(next(payloads))
        response.headers = {}
        return response

    monkeypatch.setattr(catalog, "_OPENER", SimpleNamespace(open=upstream))
    fetch = lambda: catalog.request_json(SimpleNamespace())
    with pytest.raises(ClaudapterError):
        fetch()
    assert fetch()["data"][0]["id"] == "fixture-model"


def test_truncated_provider_response_has_typed_error_and_closes(tmp_path, monkeypatch):
    _config, _provider = config_and_provider(tmp_path, monkeypatch)

    class Broken(io.BytesIO):
        def __init__(self):
            super().__init__()
            self.headers = {}

        def read(self, size=-1):
            raise IncompleteRead(b"synthetic-partial", 1)

    response = Broken()
    monkeypatch.setattr(catalog, "_OPENER", SimpleNamespace(open=lambda *args, **kwargs: response))
    with pytest.raises(ClaudapterError) as caught:
        catalog.request_json(SimpleNamespace())
    assert "synthetic-partial" not in str(caught.value)
    assert response.closed

