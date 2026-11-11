import json
from unittest.mock import patch

import pytest

from claudapter.catalog import cached_models, fetch_models
from claudapter.config import Config, Provider, cache_path
from claudapter.errors import ClaudapterError
from claudapter.gateway_options import runtime_options, validate_gateway_runtime


def test_gateway_catalog_mapping_cache_scope_and_no_certification(tmp_path):
    pytest.importorskip("protoverta.discovery")
    from protoverta.discovery import ModelInfo

    provider = Provider("demo", "Demo", "https://example.test/api/v1", protocol="chat", execution_mode="gateway")
    config = Config(providers={"demo": provider}, path=tmp_path / "config.toml")
    with (
        patch("claudapter.catalog.get_token", return_value="fixture-one"),
        patch("protoverta.discovery.fetch_catalog", return_value=(ModelInfo("m", "Model", context_window_tokens=1000),)) as fetch,
    ):
        # Reported limits are listed as metadata; they are not a capability certification.
        assert fetch_models(config, provider) == [{"id": "m", "display_name": "Model", "context_window_tokens": 1000}]
        assert fetch.call_args.args[0] == provider.base_url
        assert cached_models(config, provider) == ([{"id": "m", "display_name": "Model", "context_window_tokens": 1000}], False)
    with patch("claudapter.catalog.get_token", return_value="fixture-two"):
        assert cached_models(config, provider) == ([], True)
    body = cache_path(config).read_text()
    assert "fixture-one" not in body and "fixture-two" not in body
    assert json.loads(body)["demo"]["model_info"][0]["capability_status"] == "unknown"


def test_new_runtime_policies_round_trip_without_importing_catalog_settings_into_gateway(tmp_path):
    values = {"catalog_policy_file": "catalog.json", "context_preflight": "estimate", "require_verified_routes": True}
    assert validate_gateway_runtime(values, "gateway") == values
    assert runtime_options(values, "gateway", tmp_path / "config.toml") == {"context_preflight": "estimate", "require_verified_routes": True}
    for bad in ({"context_preflight": "exact"}, {"require_verified_routes": 1}):
        with pytest.raises(ClaudapterError):
            validate_gateway_runtime(bad, "gateway")


def _catalog_with_invalid_entries(real_fetch):
    body = json.dumps(
        {
            "data": [
                {"id": "good", "context_length": 8000, "top_provider": {"max_completion_tokens": 1000}},
                {"id": "no spaces allowed"},
                {"id": "odd-limits", "context_length": "128k"},
            ]
        }
    ).encode()

    def fetch(base_url, headers, policy, **options):
        assert policy.invalid_entries == "skip"
        options["upstream_open"] = lambda request, timeout: __import__("io").BytesIO(body)
        return real_fetch(base_url, headers, policy, **options)

    return fetch


def test_gateway_catalog_skips_invalid_entries_and_says_so(tmp_path, capsys):
    discovery = pytest.importorskip("protoverta.discovery")
    provider = Provider("demo", "Demo", "https://example.test/api/v1", protocol="chat", execution_mode="gateway")
    config = Config(providers={"demo": provider}, path=tmp_path / "config.toml")
    with (
        patch("claudapter.catalog.get_token", return_value="fixture"),
        patch("protoverta.discovery.fetch_catalog", _catalog_with_invalid_entries(discovery.fetch_catalog)),
    ):
        models = fetch_models(config, provider)
    assert [item["id"] for item in models] == ["good", "odd-limits"]
    assert models[0]["context_window_tokens"] == 8000 and "context_window_tokens" not in models[1]
    assert "skipped 1 invalid entry" in capsys.readouterr().err
