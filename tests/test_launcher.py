import tempfile
import sys
import types
import unittest
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from unittest.mock import patch

from claudapter.config import Config, Provider
from claudapter.credentials import store_token
from claudapter.launcher import build_invocation, launch


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

    def test_gateway_mode_uses_local_bearer_without_exposing_upstream_key(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Provider("demo", "Demo", "https://gateway.test", "api_key", execution_mode="gateway")
            config = Config({"demo": p}, path=Path(directory) / "config.toml")
            store_token(config, "demo", "upstream-secret")
            with patch("claudapter.launcher.shutil.which", return_value="claude.exe"):
                args, env = build_invocation(
                    config, p, "claude-demo", "default", [],
                    gateway_url="http://127.0.0.1:8765", gateway_token="local-secret",
                )
            self.assertEqual(args, ["claude.exe", "--model", "claude-demo"])
            self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8765")
            self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "local-secret")
            self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_gateway_closes_when_claude_process_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Provider("demo", "Demo", "https://gateway.test", execution_mode="gateway")
            config = Config({"demo": p}, path=Path(directory) / "config.toml")
            store_token(config, "demo", "upstream-secret")
            events = []

            class Protocol(StrEnum):
                MESSAGES = "messages"

            class GatewayConfig:
                def __init__(self, upstream, base_url, key, inbound_token, auth_style, **policy):
                    assert policy == {"upstream_reasoning": "provider_default"}
                    self.inbound_token = inbound_token
                    events.append((upstream, base_url, key, auth_style))

            @contextmanager
            def gateway_session(_config):
                events.append("open")
                try:
                    yield "http://127.0.0.1:8765"
                finally:
                    events.append("closed")

            package = types.ModuleType("protoverta")
            package.__path__ = []
            core = types.ModuleType("protoverta.core")
            core.Protocol = Protocol
            server = types.ModuleType("protoverta.server")
            server.GatewayConfig = GatewayConfig
            server.gateway_session = gateway_session
            with patch.dict(sys.modules, {"protoverta": package, "protoverta.core": core, "protoverta.server": server}), patch(
                "claudapter.launcher.shutil.which", return_value="claude.exe"
            ), patch("claudapter.launcher.subprocess.run", return_value=types.SimpleNamespace(returncode=3)) as run:
                self.assertEqual(launch(config, p, "claude-demo", "default", []), 3)
            self.assertEqual(events[0], ("messages", "https://gateway.test/v1", "upstream-secret", "bearer"))
            self.assertEqual(events[1:], ["open", "closed"])
            self.assertEqual(run.call_args.kwargs["env"]["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8765")


if __name__ == "__main__":
    unittest.main()
