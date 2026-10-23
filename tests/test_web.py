import json
import queue
import re
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from claudapter.config import Config, Provider, load
from claudapter.credentials import get_token, store_token
from claudapter.web import _HTML, persist_provider_change, serve


class WebTests(unittest.TestCase):
    def test_failed_key_write_does_not_save_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(path=Path(directory) / "config.toml")
            updated = Config({"demo": Provider("demo", "Demo", "https://gateway.test")}, path=config.path)
            with patch("claudapter.web.store_token", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    persist_provider_change(config, updated, "demo", "new-secret")
            self.assertEqual(config.providers, {})
            self.assertFalse(config.path.exists())

    def test_failed_config_save_restores_previous_key_and_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            old = Provider("demo", "Old", "https://gateway.test")
            config = Config({"demo": old}, path=Path(directory) / "config.toml")
            store_token(config, "demo", "old-secret")
            updated = Config({"demo": Provider("demo", "New", "https://gateway.test")}, path=config.path)
            with patch("claudapter.web.save", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    persist_provider_change(config, updated, "demo", "new-secret")
            self.assertEqual(config.providers["demo"].display_name, "Old")
            self.assertEqual(get_token(config, "demo"), "old-secret")
            with patch("claudapter.web.save", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    persist_provider_change(config, Config(path=config.path), "demo", "")
            self.assertIn("demo", config.providers)
            self.assertEqual(get_token(config, "demo"), "old-secret")

    def test_brand_uses_full_artwork_and_icon_favicon_for_both_themes(self):
        self.assertIn('id="brandLogo" src="/assets/ClaudApter_black.png"', _HTML)
        self.assertIn('id="favicon" rel="icon" type="image/png" href="/assets/ClaudApter_black_icon.png"', _HTML)
        self.assertIn("`/assets/ClaudApter_${light?'white':'black'}.png`", _HTML)
        self.assertIn("`/assets/ClaudApter_${light?'white':'black'}_icon.png`", _HTML)

    def test_provider_editor_is_inline_instead_of_a_modal(self):
        self.assertIn('<form id="providerForm" class="form hidden">', _HTML)
        self.assertNotIn('<dialog', _HTML)
        self.assertNotIn('showModal()', _HTML)
        element_ids = set(re.findall(r'id="([^"]+)"', _HTML))
        referenced_ids = set(re.findall(r"\$\('([^']+)'\)", _HTML))
        self.assertFalse(referenced_ids - element_ids)

    def test_setup_run_hands_terminal_to_claude(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = Provider("demo", "Demo", "https://gateway.test")
            config = Config({"demo": provider}, path=Path(directory) / "config.toml")
            store_token(config, "demo", "secret")
            opened = queue.Queue()
            launched_on = []
            result = {}

            def fake_launch(*_args):
                launched_on.append(threading.get_ident())
                return 0

            with patch("claudapter.web.webbrowser.open", side_effect=opened.put), patch("claudapter.web.preflight", return_value=None), patch("claudapter.web.launch", side_effect=fake_launch):
                worker = threading.Thread(target=lambda: result.setdefault("code", serve(config)))
                worker.start()
                url = opened.get(timeout=5)
                try:
                    with self.assertRaises(HTTPError) as denied:
                        urlopen(url.split("?", 1)[0], timeout=5)
                    self.assertEqual(denied.exception.code, 403)
                    with urlopen(url, timeout=5) as response:
                        self.assertIn("img-src 'self'", response.headers.get("Content-Security-Policy", ""))
                        page = response.read().decode()
                    token = re.search(r"const TOKEN='([^']+)'", page).group(1)
                    for extra_fields in ({"upstream_reasoning": "openrouter_disabled"}, {}):
                        edit = Request(url.split("?", 1)[0] + "api/provider", data=json.dumps({"id": "demo", "display_name": "Demo renamed", "base_url": "https://gateway.test", "execution_mode": "gateway", **extra_fields}).encode(), headers={"Content-Type": "application/json", "X-Claudapter-Token": token}, method="POST")
                        with urlopen(edit, timeout=5) as response:
                            self.assertEqual(json.load(response)["providers"][0]["upstream_reasoning"], "openrouter_disabled")
                        self.assertEqual(load(config.path).providers["demo"].upstream_reasoning, "openrouter_disabled")
                    for name in ("ClaudApter_black.png", "ClaudApter_white.png", "ClaudApter_black_icon.png", "ClaudApter_white_icon.png"):
                        with urlopen(url.split("?", 1)[0] + "assets/" + name, timeout=5) as asset:
                            self.assertEqual(asset.headers.get("Content-Type"), "image/png")
                            self.assertEqual(asset.read(8), b"\x89PNG\r\n\x1a\n")
                    endpoint = url.split("?", 1)[0] + "api/run"
                    request = Request(endpoint, data=json.dumps({"provider": "demo", "model": "claude-demo", "effort": "high"}).encode(), headers={"Content-Type": "application/json", "X-Claudapter-Token": token}, method="POST")
                    self.assertEqual(json.load(urlopen(request, timeout=5)), {"ready": True})
                    worker.join(timeout=5)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(result["code"], 0)
                    self.assertEqual(launched_on, [worker.ident])
                finally:
                    if worker.is_alive():
                        # A failing assertion must not leave a test HTTP server running.
                        request = Request(url.split("?", 1)[0] + "api/run", data=json.dumps({"provider": "demo", "model": "claude-demo", "effort": "high"}).encode(), headers={"Content-Type": "application/json", "X-Claudapter-Token": token}, method="POST")
                        urlopen(request, timeout=5).close()
                        worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
