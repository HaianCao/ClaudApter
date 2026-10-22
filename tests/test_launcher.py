import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claudapter.config import Config, Provider
from claudapter.credentials import store_token
from claudapter.launcher import build_invocation


class LauncherTests(unittest.TestCase):
    def test_bearer_and_session_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Provider("demo", "Demo", "https://gateway.test")
            config = Config({"demo": p}, path=Path(directory) / "config.toml")
            store_token(config, "demo", "secret")
            with patch("claudapter.launcher.shutil.which", return_value="claude.exe"), patch.dict("os.environ", {"ANTHROPIC_API_KEY": "wrong", "ANTHROPIC_CUSTOM_HEADERS": "Authorization: Bearer wrong", "CLAUDE_CODE_USE_BEDROCK": "1"}):
                args, env = build_invocation(config, p, "claude-demo", "high", ["session-id"], "resume")
            self.assertEqual(args, ["claude.exe", "--model", "claude-demo", "--effort", "high", "--resume", "session-id"])
            self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://gateway.test")
            self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "secret")
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertNotIn("ANTHROPIC_CUSTOM_HEADERS", env)
            self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", env)

    def test_api_key_header_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Provider("demo", "Demo", "https://gateway.test", "api_key")
            config = Config({"demo": p}, path=Path(directory) / "config.toml")
            store_token(config, "demo", "secret")
            with patch("claudapter.launcher.shutil.which", return_value="claude.exe"):
                args, env = build_invocation(config, p, "claude-demo", "default", ["hello"], "print")
            self.assertEqual(args, ["claude.exe", "--model", "claude-demo", "-p", "hello"])
            self.assertEqual(env["ANTHROPIC_API_KEY"], "secret")
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)


if __name__ == "__main__":
    unittest.main()
