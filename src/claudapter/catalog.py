from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import Config, Provider, cache_path
from .credentials import get_token
from .errors import ClaudapterError


def _url(provider: Provider, suffix: str) -> str:
    return provider.base_url.rstrip("/") + suffix


def _headers(config: Config, provider: Provider) -> dict[str, str]:
    token = get_token(config, provider.id)
    if not token:
        raise ClaudapterError(f"No API key is saved for {provider.display_name}.")
    headers = {"anthropic-version": "2023-06-01", "accept": "application/json"}
    headers["Authorization" if provider.auth_mode == "bearer" else "x-api-key"] = f"Bearer {token}" if provider.auth_mode == "bearer" else token
    return headers


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise ClaudapterError("Provider redirected the API request; refusing to forward your key.")


_OPENER = urllib.request.build_opener(_NoRedirect())


def request_json(request: urllib.request.Request, timeout: float = 12) -> dict:
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            try:
                declared_size = int(response.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ClaudapterError("Provider returned an invalid Content-Length header.") from exc
            if declared_size > 8_000_000:
                raise ClaudapterError("Provider response is too large.")
            payload = response.read(8_000_001)
            if len(payload) > 8_000_000:
                raise ClaudapterError("Provider response is too large.")
            data = json.loads(payload)
            if not isinstance(data, dict):
                raise ClaudapterError("Provider returned an invalid JSON object.")
            return data
    except urllib.error.HTTPError as exc:
        raise ClaudapterError(f"Provider returned HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, ValueError, UnicodeError) as exc:
        raise ClaudapterError(f"Provider request failed: {exc}") from exc


def fetch_models(config: Config, provider: Provider) -> list[dict[str, str]]:
    # Anthropic's API uses cursor pagination; a gateway may provide fewer pages.
    models: dict[str, dict[str, str]] = {}
    cursor = ""
    for _ in range(100):
        query = {"limit": "1000"}
        if cursor:
            query["after_id"] = cursor
        request = urllib.request.Request(_url(provider, "/v1/models") + "?" + urllib.parse.urlencode(query), headers=_headers(config, provider))
        data = request_json(request)
        entries = data.get("data")
        if not isinstance(entries, list):
            raise ClaudapterError("Provider model catalog has no data array.")
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]:
                name = entry.get("display_name")
                models[entry["id"]] = {"id": entry["id"], "display_name": name if isinstance(name, str) and name else entry["id"]}
        if not data.get("has_more"):
            break
        next_cursor = data.get("last_id")
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
            raise ClaudapterError("Provider model catalog has invalid pagination.")
        cursor = next_cursor
    else:
        raise ClaudapterError("Provider model catalog exceeds 100 pages.")
    result = list(models.values())
    try:
        path = cache_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(existing, dict):
            existing = {}
        existing[provider.id] = {"base_url": provider.base_url, "saved_at": time.time(), "models": result}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".models-", suffix=".tmp", delete=False) as stream:
                temporary = stream.name
                json.dump(existing, stream, ensure_ascii=False)
            os.replace(temporary, path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
    except (OSError, ValueError):
        pass  # Catalog caching must never break a successful live discovery.
    return result


def cached_models(config: Config, provider: Provider) -> tuple[list[dict[str, str]], bool]:
    try:
        data = json.loads(cache_path(config).read_text(encoding="utf-8"))
        entry = data.get(provider.id, {})
        if entry.get("base_url") != provider.base_url or not isinstance(entry.get("models"), list):
            return [], True
        if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"] or not isinstance(item.get("display_name"), str) for item in entry["models"]):
            return [], True
        return entry["models"], time.time() - float(entry.get("saved_at", 0)) > 86_400
    except (OSError, ValueError, TypeError, AttributeError):
        return [], True


def probe_messages(config: Config, provider: Provider, model: str) -> dict:
    payload = json.dumps({"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "."}], "stream": False}).encode("utf-8")
    headers = _headers(config, provider)
    headers["content-type"] = "application/json"
    result = request_json(urllib.request.Request(_url(provider, "/v1/messages"), data=payload, headers=headers, method="POST"), timeout=30)
    if not isinstance(result.get("content"), list):
        raise ClaudapterError("Provider did not return a Messages-compatible response.")
    return result
