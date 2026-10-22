import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claudapter.catalog import cached_models, fetch_models, probe_messages, request_json
from claudapter.config import Config, Provider
from claudapter.credentials import store_token
from claudapter.errors import ClaudapterError


class _Response:
    headers = {}

    def __init__(self, data):
        self.data = io.BytesIO(json.dumps(data).encode())

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size):
        return self.data.read(size)


class CatalogTests(unittest.TestCase):
    def test_malformed_content_length_is_reported_cleanly(self):
        response = _Response({"data": []})
        response.headers = {"Content-Length": "not-a-number"}
        with patch("claudapter.catalog._OPENER.open", return_value=response):
            with self.assertRaisesRegex(ClaudapterError, "Content-Length"):
                request_json(None)

    def test_model_names_are_normalized_and_bad_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = Provider("demo", "Demo", "https://gateway.test")
            config = Config({"demo": provider}, path=Path(directory) / "config.toml")
            store_token(config, "demo", "secret")
            with patch("claudapter.catalog._OPENER.open", return_value=_Response({"data": [{"id": "claude-one", "display_name": 123}]})):
                self.assertEqual(fetch_models(config, provider), [{"id": "claude-one", "display_name": "claude-one"}])
            from claudapter.config import cache_path
            cache_path(config).write_text(json.dumps({"demo": {"base_url": provider.base_url, "models": [{"id": "claude-one", "display_name": 123}]}}), encoding="utf-8")
            self.assertEqual(cached_models(config, provider), ([], True))

    def test_paginated_model_list_and_url(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = Provider("demo", "Demo", "https://gateway.test/prefix")
            config = Config({"demo": provider}, path=Path(directory) / "config.toml")
            store_token(config, "demo", "secret")
            pages = [_Response({"data": [{"id": "claude-one", "display_name": "One"}], "has_more": True, "last_id": "claude-one"}), _Response({"data": [{"id": "claude-two"}], "has_more": False})]
            with patch("claudapter.catalog._OPENER.open", side_effect=lambda request, timeout: (self.assertIn("/prefix/v1/models?", request.full_url), pages.pop(0))[1]):
                models = fetch_models(config, provider)
            self.assertEqual([m["id"] for m in models], ["claude-one", "claude-two"])
            self.assertEqual(cached_models(config, provider)[0], models)
            self.assertFalse(cached_models(config, provider)[1])

    def test_messages_probe_uses_correct_path(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = Provider("demo", "Demo", "https://gateway.test")
            config = Config({"demo": provider}, path=Path(directory) / "config.toml")
            store_token(config, "demo", "secret")
            with patch("claudapter.catalog._OPENER.open", side_effect=lambda request, timeout: (self.assertEqual(request.full_url, "https://gateway.test/v1/messages"), _Response({"content": []}))[1]):
                probe_messages(config, provider, "claude-one")


if __name__ == "__main__":
    unittest.main()
