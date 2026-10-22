import tempfile
import unittest
from pathlib import Path

from claudapter.config import Config, Provider, load, save, secret_var, validate_base_url
from claudapter.credentials import get_token, remove_token, store_token
from claudapter.errors import ClaudapterError


class ConfigTests(unittest.TestCase):
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

    def test_url_must_be_gateway_root(self):
        self.assertEqual(validate_base_url("https://example.test/api/"), "https://example.test/api")
        for value in ("http://example.test", "https://example.test/v1", "https://example.test/v1/messages", "https://user:pass@example.test", "https://example.test?key=secret", "https://[invalid", "https://example.test:bad", "https://:443"):
            with self.subTest(value=value), self.assertRaises(ClaudapterError):
                validate_base_url(value)

    def test_secret_name(self):
        self.assertEqual(secret_var("agent-router"), "AGENT_ROUTER_TOKEN")

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
