"""Model refresh does not hold the settings lock or commit stale cache data."""

import importlib
import json
import threading
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest
from test_web_boundary import listener

PACKAGE = "claudapter"
web = importlib.import_module(PACKAGE + ".web")
catalog = importlib.import_module(PACKAGE + ".catalog")


@pytest.mark.parametrize("change", ["provider", "key", "mode", "delete", "external", "default", "run"])
def test_refresh_is_unlocked_and_late_cache_is_identity_checked(tmp_path, monkeypatch, change):
    codex = PACKAGE == "codex_adapter"
    monkeypatch.setattr(PACKAGE + ".launcher.shutil.which", lambda _: None)
    monkeypatch.setenv(("CODEX_CA" if codex else "CLAUDE_CA") + "_CACHE", str(tmp_path / "cache.json"))
    monkeypatch.setenv("FIXTURE_TOKEN", "synthetic-original")
    started, release, launched = threading.Event(), threading.Event(), threading.Event()
    results, snapshots = [], []

    def fetch(config, provider, *, cache_sink, budget, token):
        identity = catalog._identity(provider, config, token=token) if codex else catalog._identity(config, provider, token=token)
        snapshots.append((provider.base_url, token))
        started.set()
        while not release.wait(0.02):
            budget.remaining()
        budget.remaining()
        models = ["old-model"] if codex else [{"id": "old-model", "display_name": "Old"}]
        cache_sink({"identity": identity, "models": models, "fetched_at" if codex else "saved_at": 1})
        return models

    def fake_launch(*_):
        launched.set()
        return 0

    # Keep launch mocked until listener teardown has joined the setup worker.
    with patch.object(web, "fetch_models", side_effect=fetch), patch.object(web, "preflight"), patch.object(web, "launch", side_effect=fake_launch) as launch, listener(tmp_path, monkeypatch) as (_, url, session, _):
        origin = f"http://{url.netloc}"
        headers = {"Content-Type": "application/json", "X-Codex-Apter-Token" if codex else "X-Claudapter-Token": session}

        def call(path, body=None, method=None):
            req = Request(origin + path, data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method)
            try:
                with urlopen(req, timeout=2) as response:
                    return response.status, json.load(response)
            except HTTPError as error:
                with error:
                    return error.code, json.load(error)
            except (OSError, URLError):
                return None, None  # Run may close the setup socket after cancel.

        body = {"id": "fixture", "display_name": "Fixture", "base_url": "https://old.test/v1", "api_key": "synthetic-original"}
        assert call("/api/provider", body)[0] == 200
        worker = threading.Thread(target=lambda: results.append(call("/api/models/refresh", {"provider": "fixture"})))
        worker.start()
        try:
            assert started.wait(2)
            if change in {"provider", "key", "mode"}:
                edit = {**body, **({"base_url": "https://new.test/v1"} if change == "provider" else {"api_key": "synthetic-replacement"} if change == "key" else {"execution_mode": "gateway"})}
                assert call("/api/provider", edit)[0] == 200
            elif change == "delete":
                assert call("/api/provider?provider=fixture", method="DELETE")[0] == 200
            elif change == "external":
                with (tmp_path / "config.toml").open("a") as file:
                    file.write("\n# changed elsewhere\n")
            else:
                selection = {"provider": "fixture", "model": "manual", "reasoning" if codex else "effort": "medium" if codex else "default"}
                assert call("/api/" + change, selection)[0] == 200
            release.set()
            worker.join(3)
            assert not worker.is_alive()
            assert snapshots == [("https://old.test/v1", "synthetic-original")]
            if change == "default":
                assert results[0][0] == 200
                assert (tmp_path / "cache.json").exists()
            else:
                assert results[0][0] in {None, 400, 408}
                assert not (tmp_path / "cache.json").exists()
            if change == "run":
                assert launched.wait(3)
                launch.assert_called_once()
            else:
                launch.assert_not_called()
        finally:
            release.set()
            worker.join(3)
