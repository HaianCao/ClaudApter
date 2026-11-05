import io
import json
from http.client import IncompleteRead
from types import SimpleNamespace

import pytest

from claudapter import catalog
from claudapter.config import Config, Provider
from claudapter.errors import ClaudapterError

INVALID = [b'{"x":' + b"1" * 5000 + b"}", b'{"x":' + b"[" * 10000 + b"0" + b"]" * 10000 + b"}", b'{"x":"\\ud800"}', b'{"x":NaN}', b'{"x":1e309}', b"{", b"\xff"]


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


@pytest.mark.parametrize(
    "saved_at,stale",
    [
        (1_700_000_000, False),
        (1_699_999_999.5, False),
        (1_700_000_000 - 86_400, False),
        (1_700_000_000 - 86_401, True),
        ("nan", True),
        ("inf", True),
        ("-inf", True),
        ("1700000000", True),
        (True, True),
        (False, True),
        (None, True),
        ([], True),
        ({}, True),
        (-1, True),
        (-0.5, True),
        (1_700_000_001, True),
        (10**500, True),
    ],
    ids=[
        "fresh-int",
        "fresh-float",
        "ttl-boundary",
        "expired",
        "nan-string",
        "inf-string",
        "negative-inf-string",
        "numeric-string",
        "true",
        "false",
        "null",
        "array",
        "object",
        "negative-int",
        "negative-float",
        "future",
        "huge-int",
    ],
)
def test_cache_timestamp_validation_preserves_models_but_marks_invalid_age_stale(tmp_path, monkeypatch, saved_at, stale):
    config, provider = config_and_provider(tmp_path, monkeypatch)
    path = tmp_path / "cache.json"
    monkeypatch.setattr(catalog, "cache_path", lambda *args: path)
    monkeypatch.setattr(catalog.time, "time", lambda: 1_700_000_000)
    models = [{"id": "fixture-model", "display_name": "Fixture Model"}]
    raw = json.dumps({provider.id: {"identity": catalog._identity(config, provider), "models": models, "saved_at": saved_at}}).encode()
    path.write_bytes(raw)
    assert catalog.cached_models(config, provider) == (models, stale)
    assert path.read_bytes() == raw  # Reading stale cache must not rewrite user state.


def test_missing_cache_timestamp_is_stale(tmp_path, monkeypatch):
    config, provider = config_and_provider(tmp_path, monkeypatch)
    path = tmp_path / "cache.json"
    monkeypatch.setattr(catalog, "cache_path", lambda *args: path)
    models = [{"id": "fixture-model", "display_name": "Fixture Model"}]
    path.write_text(json.dumps({provider.id: {"identity": catalog._identity(config, provider), "models": models}}), encoding="utf-8")
    assert catalog.cached_models(config, provider) == (models, True)


@pytest.mark.parametrize("saved_at", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_json_timestamp_still_rejects_entire_invalid_json_cache(tmp_path, monkeypatch, saved_at):
    config, provider = config_and_provider(tmp_path, monkeypatch)
    path = tmp_path / "cache.json"
    monkeypatch.setattr(catalog, "cache_path", lambda *args: path)
    path.write_text(json.dumps({provider.id: {"identity": catalog._identity(config, provider), "models": [], "saved_at": saved_at}}), encoding="utf-8")
    assert catalog.cached_models(config, provider) == ([], True)


@pytest.mark.parametrize(
    "raw", [*INVALID, b"[]", b"null"], ids=["huge-integer", "deep-nesting", "surrogate", "nan", "overflow", "truncated-json", "invalid-utf8", "array", "null"]
)
def test_successful_refresh_replaces_bad_cache_and_survives_reopen(tmp_path, monkeypatch, raw):
    config, provider = config_and_provider(tmp_path, monkeypatch)
    path = tmp_path / "cache.json"
    path.write_bytes(raw)
    config.path.write_text("configuration sentinel", encoding="utf-8")
    monkeypatch.setattr(catalog, "cache_path", lambda *args: path)
    monkeypatch.setattr(catalog, "get_token", lambda *args, **kwargs: "synthetic-token")

    def upstream(*args, **kwargs):
        response = io.BytesIO(b'{"data":[{"id":"fixture-model","display_name":"Fixture Model"}]}')
        response.headers = {}
        return response

    monkeypatch.setattr(catalog, "_OPENER", SimpleNamespace(open=upstream))
    assert catalog.cached_models(config, provider) == ([], True)
    models = catalog.fetch_models(config, provider)
    reopened = Config(path=config.path)
    assert catalog.cached_models(reopened, provider) == (models, False)
    saved = path.read_text(encoding="utf-8")
    assert json.loads(saved)[provider.id]["models"] == models
    assert "synthetic-token" not in saved
    assert config.path.read_text(encoding="utf-8") == "configuration sentinel"
    assert not list(tmp_path.glob(".models-*.tmp"))


def test_cache_update_preserves_other_provider_entries(tmp_path, monkeypatch):
    config, provider = config_and_provider(tmp_path, monkeypatch)
    path = tmp_path / "cache.json"
    other = {"models": [{"id": "other-model", "display_name": "Other"}]}
    path.write_text(json.dumps({"other": other}), encoding="utf-8")
    monkeypatch.setattr(catalog, "cache_path", lambda *args: path)
    catalog.store_catalog(config, provider.id, {"models": []})
    assert json.loads(path.read_text(encoding="utf-8")) == {"other": other, provider.id: {"models": []}}


def test_cache_read_permission_error_does_not_replace_existing_file(tmp_path, monkeypatch):
    config, provider = config_and_provider(tmp_path, monkeypatch)
    path = tmp_path / "cache.json"
    original = b'{"other":{"models":[]}}'
    path.write_bytes(original)
    monkeypatch.setattr(catalog, "cache_path", lambda *args: path)
    read_text = type(path).read_text

    def unreadable(target, *args, **kwargs):
        if target == path:
            raise PermissionError("synthetic read failure")
        return read_text(target, *args, **kwargs)

    monkeypatch.setattr(type(path), "read_text", unreadable)
    catalog.store_catalog(config, provider.id, {"models": []})
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".models-*.tmp"))


@pytest.mark.parametrize("fail_replace", [False, True])
def test_corrupt_cache_replacement_is_atomic_and_cleans_temporary_file(tmp_path, monkeypatch, fail_replace):
    config, provider = config_and_provider(tmp_path, monkeypatch)
    path = tmp_path / "cache.json"
    original = b"{invalid"
    path.write_bytes(original)
    monkeypatch.setattr(catalog, "cache_path", lambda *args: path)
    replace = catalog.os.replace
    expected = {provider.id: {"models": []}}
    attempted = []

    def publish(source, target):
        assert target == path
        assert path.read_bytes() == original
        assert json.loads(type(path)(source).read_text(encoding="utf-8")) == expected
        attempted.append(source)
        if fail_replace:
            raise PermissionError("synthetic replace failure")
        replace(source, target)

    monkeypatch.setattr(catalog.os, "replace", publish)
    catalog.store_catalog(config, provider.id, {"models": []})
    assert len(attempted) == 1
    if fail_replace:
        assert path.read_bytes() == original
    else:
        assert json.loads(path.read_text(encoding="utf-8")) == expected
    assert not list(tmp_path.glob(".models-*.tmp"))


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
