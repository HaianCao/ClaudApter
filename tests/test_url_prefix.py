from unittest.mock import patch

import pytest

from claudapter.catalog import _url
from claudapter.config import Config, Provider, load, save
from claudapter.launcher import build_invocation
from claudapter.web import _HTML


@pytest.mark.parametrize("url", ["https://gateway.test", "https://gateway.test/v1", "https://gateway.test/v1/"])
def test_v1_is_appended_exactly_once_for_models_messages_and_cli(url):
    provider = Provider("demo", "Demo", url)
    assert _url(provider, "/v1/models") == "https://gateway.test/v1/models"
    assert _url(provider, "/v1/messages") == "https://gateway.test/v1/messages"
    with patch("claudapter.launcher.shutil.which", return_value="claude.exe"), patch("claudapter.launcher.get_token", return_value="fixture"):
        _, env = build_invocation(Config(), provider, "m", "default", [])
    assert env["ANTHROPIC_BASE_URL"] == "https://gateway.test"


def test_nested_prefix_survives_save_reload_and_request_join(tmp_path):
    provider = Provider("demo", "Demo", "https://gateway.test/api/v1")
    config = Config({"demo": provider}, path=tmp_path / "config.toml")
    save(config)
    restored = load(config.path).providers["demo"]
    assert restored.base_url == provider.base_url
    assert _url(restored, "/v1/messages") == "https://gateway.test/api/v1/messages"


def test_setup_shows_persistent_url_example_and_bearer_recommendation():
    assert '<code>https://provider.example/v1</code>' in _HTML
    assert 'Bearer token (recommended)' in _HTML
    assert 'without the Bearer prefix' in _HTML
    assert 'Enter the URL before /v1' not in _HTML
