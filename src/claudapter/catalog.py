from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import Config, Provider, cache_path, claude_base_url
from .credentials import get_token
from .errors import ClaudapterError


def _url(provider: Provider, suffix: str) -> str:
    if provider.protocol == "messages":
        return claude_base_url(provider.base_url) + suffix
    if suffix.startswith("/v1/"):
        suffix = suffix[3:]
    return provider.base_url.rstrip("/") + suffix


def _headers(config: Config, provider: Provider) -> dict[str, str]:
    token = get_token(config, provider.id)
    if not token:
        raise ClaudapterError(f"No API key is saved for {provider.display_name}.")
    headers = {"accept": "application/json"}
    if provider.protocol == "messages":
        headers["anthropic-version"] = "2023-06-01"
    headers["Authorization" if provider.auth_mode == "bearer" else "x-api-key"] = f"Bearer {token}" if provider.auth_mode == "bearer" else token
    return headers


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        if fp is not None:
            fp.close()
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
        exc.close()
        raise ClaudapterError(f"Provider returned HTTP {exc.code}.") from None
    except (urllib.error.URLError, TimeoutError, ValueError, UnicodeError) as exc:
        raise ClaudapterError(f"Provider request failed: {type(exc).__name__}.") from None


def _identity(config, provider):
    from .gateway_options import catalog_policy_fingerprint

    return {"base_url": provider.base_url, "protocol": provider.protocol, "auth_mode": provider.auth_mode,
            "credential_fingerprint": hashlib.sha256((get_token(config, provider.id) or "").encode()).hexdigest(),
            "catalog_policy_file": provider.gateway_runtime.get("catalog_policy_file", ""),
            "catalog_policy_fingerprint": catalog_policy_fingerprint(config.path, provider)}


def _gateway_models(config, provider):
    try:
        from protoverta.discovery import CatalogPolicy, fetch_catalog

        policy = CatalogPolicy(pagination="after_id" if provider.protocol == "messages" else "none")
        policy_file = provider.gateway_runtime.get("catalog_policy_file")
        if policy_file:
            path = Path(policy_file).expanduser()
            if not path.is_absolute():
                path = (config.path.parent if config.path else Path.cwd()) / path
            policy = CatalogPolicy.load(path, provider.base_url)
        return [item.as_dict() for item in fetch_catalog(provider.base_url, _headers(config, provider), policy)]
    except ImportError:
        raise ClaudapterError("Gateway discovery requires the matching Protoverta preview.") from None
    except (ValueError, OSError, TypeError):
        raise ClaudapterError("Gateway catalog failed validation or exceeded its resource budget.") from None


def fetch_models(config: Config, provider: Provider) -> list[dict[str, str]]:
    # Anthropic's API uses cursor pagination; a gateway may provide fewer pages.
    models: dict[str, dict[str, str]] = {}
    gateway_info = _gateway_models(config, provider) if provider.execution_mode == "gateway" else None
    if gateway_info is not None:
        models = {item["id"]: {"id": item["id"], "display_name": item["display_name"]} for item in gateway_info}
    cursor = ""
    for _ in range(0 if gateway_info is not None else 100):
        query = {"limit": "1000"} if provider.protocol == "messages" else {}
        if cursor:
            query["after_id"] = cursor
        request = urllib.request.Request(_url(provider, "/v1/models") + ("?" + urllib.parse.urlencode(query) if query else ""), headers=_headers(config, provider))
        data = request_json(request)
        entries = data.get("data")
        if not isinstance(entries, list):
            raise ClaudapterError("Provider model catalog has no data array.")
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]:
                name = entry.get("display_name") or entry.get("name")
                models[entry["id"]] = {"id": entry["id"], "display_name": name if isinstance(name, str) and name else entry["id"]}
        if not data.get("has_more"):
            break
        next_cursor = data.get("last_id")
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
            raise ClaudapterError("Provider model catalog has invalid pagination.")
        cursor = next_cursor
    else:
        if gateway_info is None:
            raise ClaudapterError("Provider model catalog exceeds 100 pages.")
    result = list(models.values())
    try:
        path = cache_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(existing, dict):
            existing = {}
        existing[provider.id] = {"identity": _identity(config, provider), "saved_at": time.time(), "models": result, "model_info": gateway_info}
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
        if entry.get("identity") != _identity(config, provider) or not isinstance(entry.get("models"), list):
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
