import json

import pytest

from claudapter.config import Config, Provider, load, save, validate_base_url
from claudapter.errors import ClaudapterError


@pytest.mark.parametrize("value", [
    "https://example.test/with space", "https://exa mple.test/v1",
    " https://example.test/v1", "https://example.test/v1\n",
    "\0https://example.test/v1", "https://example.test/v1\0",
    "https://example.test/\tpath", "https://example.test/\rpath",
    "https://example.test/\x1fpath", "https://example.test/\x7fpath",
    "https://example.test/\u00a0path", "https://example.test:0/v1",
    "http://127.0.0.1:0/v1", "https://example.test:65536/v1",
    "https://example.test:-1/v1",
])
def test_invalid_url_is_rejected_without_overwriting_config(tmp_path, value):
    with pytest.raises(ClaudapterError):
        validate_base_url(value)
    path = tmp_path / "config.toml"
    original = f'schema_version = 1\n[[providers]]\nid="fixture"\ndisplay_name="Fixture"\nbase_url={json.dumps(value)}\n'
    path.write_text(original)
    with pytest.raises(ClaudapterError):
        load(path)
    with pytest.raises(ClaudapterError):
        save(Config({"fixture": Provider("fixture", "Fixture", value)}, path=path))
    assert path.read_text() == original


@pytest.mark.parametrize("value", ["https://example.test:1/v1", "https://example.test:65535/api/v1", "https://example.test/encoded%20path/v1", "http://[::1]:12345/v1"])
def test_valid_ports_encoded_paths_and_ipv6_are_preserved(value):
    assert validate_base_url(value) == value
