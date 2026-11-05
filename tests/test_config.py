import tempfile
import unittest
from pathlib import Path

from claudapter.config import (
    Config,
    Provider,
    claude_base_url,
    load,
    save,
    secret_var,
    validate_base_url,
)
from claudapter.credentials import get_token, remove_token, store_token
from claudapter.errors import ClaudapterError


class ConfigTests(unittest.TestCase):
    def test_delete_character_round_trips_as_loadable_toml(self):
        # JSON leaves U+007F raw, which TOML forbids; saved text must reload.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            provider = Provider("demo", "Demo\x7fName", "https://example.test/api", "bearer", "model\x7fid",
                                "gateway", "chat", "auto", agent_models={"policy": "custom", "default": "child-model"})
            config = Config({"demo": provider}, "demo", "model\x7fid", "default", path)
            save(config)
            self.assertNotIn("\x7f", path.read_text(encoding="utf-8"))
            self.assertEqual(load(path), config)

    def test_round_trip_and_secret_separation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            config = Config({"my_gateway": Provider("my_gateway", "My Gateway", "https://example.test/api", "api_key", "claude-demo")}, "my_gateway", "claude-demo", "high", path)
            save(config)
            store_token(config, "my_gateway", "sensitive-token")
            self.assertNotIn("sensitive-token", path.read_text())
            self.assertEqual(load(path), config)
            self.assertEqual(get_token(config, "my_gateway"), "sensitive-token")
            remove_token(config, "my_gateway")
            self.assertFalse(get_token(config, "my_gateway"))

    def test_url_accepts_api_prefix_and_legacy_gateway_root(self):
        self.assertEqual(validate_base_url("https://example.test/api/"), "https://example.test/api")
        self.assertEqual(validate_base_url("https://example.test/api/v1/"), "https://example.test/api/v1")
        self.assertEqual(claude_base_url("https://example.test/api/v1/"), "https://example.test/api")
        self.assertEqual(claude_base_url("https://example.test/api"), "https://example.test/api")
        for value in ("http://example.test", "https://example.test/v1/messages", "https://example.test/responses", "https://example.test/chat/completions", "https://user:pass@example.test", "https://example.test?key=secret", "https://[invalid", "https://example.test:bad", "https://:443"):
            with self.subTest(value=value), self.assertRaises(ClaudapterError):
                validate_base_url(value)

    def test_secret_name(self):
        self.assertEqual(secret_var("agent-router"), "AGENT_ROUTER_TOKEN")

    def test_gateway_choice_survives_config_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            config = Config({"demo": Provider("demo", "Demo", "https://example.test", execution_mode="gateway")}, path=path)
            save(config)
            self.assertEqual(load(path).providers["demo"].execution_mode, "gateway")

    def test_secret_name_collisions_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config({
                "agent-router": Provider("agent-router", "One", "https://one.test"),
                "agent_router": Provider("agent_router", "Two", "https://two.test"),
            }, path=Path(directory) / "config.toml")
            with self.assertRaises(ClaudapterError):
                save(config)


if __name__ == "__main__":
    unittest.main()
